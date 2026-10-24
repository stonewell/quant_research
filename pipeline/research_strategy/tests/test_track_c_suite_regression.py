"""Track C suite-level regression (Task C4.1 -- the ONLY whole-suite verification of Track C).

`C1.2`-`C3.2` each shipped a single-file unit test; none of them constituted a
Track-C-wide regression conclusion, and several of those single-file tests were
*structurally vacuous* -- e.g. `test_chan_vp_hybrid.py::..._sparse_contract`
only asserted `len(non_nan_rows) > 0`, which the strategy's initial all-zero
row alone satisfies. This module is the missing suite-level net:

1. Operator-level convergence for M1-M5 (`common/structural_factors.py`) on
   single-series AND DataFrame inputs, against independent reference
   implementations, with explicit NaN-propagation rules (no NaN *diffusion*).
2. The `AGENTS.md` sparse-weights contract for the two Track C strategies
   (`ChanVolumeProfileHybridStrategy`, `WyckoffChanSpringStrategy`), verified
   on both their real data path and on scripted entry/exit transitions driven
   through their documented seam (the module-level position loop) -- because
   on generic synthetic data neither strategy ever leaves its initial row.
3. A sweep of every registered strategy class + the 9 static allocation
   templates, so "55+ strategies still work" is an actual assertion instead of
   an inference from the four suite runs.

B1.1 CONTEXT (why "actual_w == target" is no longer assertable): B1.1 turned
`run_allocation_backtest` into a real position ledger (`actual_w = position *
P / equity`, `cash >= 0`, integer-lot truncation, partial fills). Under that
engine an actual weight is *approximate* by construction: it equals the target
only when the lot size happens to divide the notional exactly. Every ledger
assertion below therefore checks INTENT (a trade happened / weight is on the
correct side of the target / the exit is exactly flat), never bit-equality
with the target -- see `test_ledger_semantics_actual_weight_never_overfills`.

100% offline: synthetic OHLCV only (`common/testing.py`), no network access.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.allocation_backtester import run_allocation_backtest
from common.allocation_templates import (
    BreadthGatedMomentumAllocation,
    CrossSectionalMomentumAllocation,
    DualMomentumAllocation,
    EqualWeightAllocation,
    HierarchicalRiskParityAllocation,
    InverseVolatilityAllocation,
    MaxDiversificationAllocation,
    MeanReversionAllocation,
    MinimumVarianceAllocation,
)
from common.structural_factors import (
    compute_rolling_poc_series,
    consolidation_density,
    cyq_chip_distribution,
    enrich_universe_with_structural_features,
    extremum_curvature,
    fractal_dimension,
    standardized_divergence,
    wyckoff_effort_result,
    wyckoff_spring,
)
from common.testing import make_ohlcv_from_closes, make_trending_pullback_df
from research_strategy.rs import chan_vp_hybrid_strategy as cvp_mod
from research_strategy.rs import wyckoff_chan_strategy as wyc_mod
from research_strategy.rs.chan_vp_hybrid_strategy import ChanVolumeProfileHybridStrategy
from research_strategy.rs.strategy import STRATEGY_CLASS_MAP
from research_strategy.rs.wyckoff_chan_strategy import WyckoffChanSpringStrategy


# --------------------------------------------------------------------------- #
# Shared synthetic fixtures
# --------------------------------------------------------------------------- #
def _walk(n=300, seed=11, drift=0.0003, vol=0.010):
    rs = np.random.RandomState(seed)
    close = 100.0 * np.exp(np.cumsum(rs.normal(drift, vol, n)))
    bars = make_ohlcv_from_closes(close)
    bars["Volume"] = 1_000.0 + rs.uniform(0.0, 800.0, n)
    bars.index = pd.bdate_range("2018-01-01", periods=n)
    return bars


def _two_symbol_universe(n=300, seed=11):
    spy = _walk(n, seed)
    bil = make_ohlcv_from_closes(100.0 + np.arange(n) * 0.001)
    bil["Volume"] = 1_000.0
    bil.index = spy.index
    return {"SPY": spy, "BIL": bil}


def _assert_sparse_contract(weights, universe_index, n_rebalance_expected=None):
    """Mechanical `AGENTS.md` sparse-weights contract checks.

    A row is either a *rebalance row* (every cell carries a real target weight,
    including explicit 0.0 for anything being de-risked) or an *all-NaN row*
    ("nothing instructed today"). Mixing the two -- i.e. leaving a cell NaN
    inside a rebalance row -- is the exact failure mode the ledger's
    column-wise `ffill()` turns into a silently carried stale position.
    """
    assert isinstance(weights, pd.DataFrame)
    if weights.empty:
        return 0
    assert weights.index.is_monotonic_increasing
    assert weights.index.isin(universe_index).all(), "weights must live on universe dates"

    per_row_nan = weights.isna().sum(axis=1)
    rebalance_rows = weights[per_row_nan == 0]
    nan_rows = weights[per_row_nan == weights.shape[1]]
    assert len(rebalance_rows) + len(nan_rows) == len(weights), (
        "every row must be either fully populated (rebalance) or fully NaN (no instruction); "
        f"found {len(weights) - len(rebalance_rows) - len(nan_rows)} mixed rows"
    )
    assert len(rebalance_rows) > 0, "strategy must produce at least one rebalance row"
    if n_rebalance_expected is not None:
        assert len(rebalance_rows) == n_rebalance_expected

    assert (rebalance_rows.values >= -1e-12).all(), "no negative target weights"
    row_sums = rebalance_rows.sum(axis=1)
    assert (row_sums <= 1.0 + 1e-9).all(), f"target weights must never exceed 1.0, max={row_sums.max()}"

    # "Must not forward-fill itself": a self-ffill'd frame has no all-NaN rows
    # at all. (Two *consecutive* rebalance rows with an identical target are
    # legitimate -- the backtester keys off row presence, not value change.)
    assert len(nan_rows) > 0, "no all-NaN rows => the template forward-filled its own output"
    return len(rebalance_rows)


def _scripted_path(index, spec):
    """Build a dense weight path from `{start_pos: weight}` transition points."""
    out = np.zeros(len(index), dtype=float)
    for start_pos, weight in spec:
        out[start_pos:] = weight
    return out


# --------------------------------------------------------------------------- #
# 1. Operator-level convergence: M1-M5 on single-series and DataFrame inputs
# --------------------------------------------------------------------------- #
def _poc_reference(bars, lookback, n_bins, value_area_pct):
    """Independent `np.add.at`-based re-implementation of the rolling volume
    profile (the module under test uses an explicit `idx_start:idx_end +=`
    slice accumulation; this reasons about the same definition with a
    vectorised histogram-style accumulation instead)."""
    high = bars["High"].to_numpy(float)
    low = bars["Low"].to_numpy(float)
    close = bars["Close"].to_numpy(float)
    vol = (
        bars["Volume"].fillna(1.0).to_numpy(float)
        if "Volume" in bars.columns and not bars["Volume"].isna().all()
        else np.ones(len(bars))
    )
    n = len(bars)
    poc = np.full(n, np.nan)
    val = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    for t in range(lookback - 1, n):
        lo = low[t - lookback + 1 : t + 1]
        hi = high[t - lookback + 1 : t + 1]
        vv = vol[t - lookback + 1 : t + 1]
        p_min, p_max = float(lo.min()), float(hi.max())
        if p_max <= p_min or np.isnan(p_min) or np.isnan(p_max):
            poc[t], val[t], vah[t] = close[t], close[t] * 0.95, close[t] * 1.05
            continue
        width = (p_max - p_min) / n_bins
        edges = np.linspace(p_min, p_max, n_bins + 1)
        mids = 0.5 * (edges[:-1] + edges[1:])
        hist = np.zeros(n_bins)
        for i in range(lookback):
            a = max(0, min(n_bins - 1, int((lo[i] - p_min) / width)))
            b = max(0, min(n_bins - 1, int((hi[i] - p_min) / width)))
            share = vv[i] / (b - a + 1)
            np.add.at(hist, np.arange(a, b + 1), share)
        k = int(np.argmax(hist))
        poc[t] = mids[k]
        target = hist.sum() * value_area_pct
        acc, lo_i, hi_i = hist[k], k, k
        while acc < target and (lo_i > 0 or hi_i < n_bins - 1):
            n_low = hist[lo_i - 1] if lo_i > 0 else 0.0
            n_high = hist[hi_i + 1] if hi_i < n_bins - 1 else 0.0
            if n_low >= n_high and lo_i > 0:
                lo_i -= 1
                acc += n_low
            elif hi_i < n_bins - 1:
                hi_i += 1
                acc += n_high
            else:
                break
        val[t] = edges[lo_i]
        vah[t] = edges[hi_i + 1]
    return poc, val, vah


def test_poc_matches_independent_histogram_reference():
    """M5/POC: rolling volume profile agrees numerically with an independent
    `np.add.at` histogram formulation, for both `poc` and the value area."""
    bars = _walk(140, seed=3)
    lookback, n_bins, va = 30, 25, 0.70
    got = compute_rolling_poc_series(bars, lookback=lookback, n_bins=n_bins, value_area_pct=va)
    ref = _poc_reference(bars, lookback, n_bins, va)
    for name, g, r in zip(("poc", "val", "vah"), got, ref):
        np.testing.assert_allclose(g, r, rtol=0, atol=1e-12, equal_nan=True, err_msg=name)


def test_poc_value_area_width_monotonic_in_coverage():
    """A wider requested value area can never *shrink* the [VAL, VAH] band."""
    bars = _walk(120, seed=4)
    prev_width = None
    for pct in (0.50, 0.70, 0.90):
        poc, val, vah = compute_rolling_poc_series(bars, lookback=25, n_bins=20, value_area_pct=pct)
        widths = (vah - val)[24:]
        assert np.all(widths >= 0.0)
        if prev_width is not None:
            assert np.all(widths >= prev_width - 1e-12), "value area must not shrink as coverage grows"
        prev_width = widths


def test_poc_boundary_inputs_never_nan_diffuse():
    """Boundary inputs: empty / single bar / shorter-than-lookback / flat price
    / all-NaN volume / NaN inside High. NaN is only ever allowed in the
    documented warmup block (`lookback - 1` bars) or where the input itself is
    degenerate -- never as an unbounded tail."""
    empty = pd.DataFrame({"High": [], "Low": [], "Close": []})
    for arr in compute_rolling_poc_series(empty, lookback=20):
        assert isinstance(arr, np.ndarray) and arr.shape == (0,)

    single = pd.DataFrame({"High": [101.0], "Low": [99.0], "Close": [100.0]})
    assert all(np.isnan(a).all() and len(a) == 1 for a in compute_rolling_poc_series(single, lookback=5))

    short = make_ohlcv_from_closes(np.linspace(100.0, 105.0, 12))
    assert all(np.isnan(a).all() for a in compute_rolling_poc_series(short, lookback=25))

    flat = pd.DataFrame(
        {"High": np.full(40, 88.0), "Low": np.full(40, 88.0), "Close": np.full(40, 88.0)}
    )
    poc, val, vah = compute_rolling_poc_series(flat, lookback=15)
    assert np.isnan(poc[:14]).all() and np.isfinite(poc[14:]).all()
    np.testing.assert_allclose(poc[14:], 88.0)
    np.testing.assert_allclose(val[14:], 88.0 * 0.95)
    np.testing.assert_allclose(vah[14:], 88.0 * 1.05)

    # NaN sprayed into High/Low falls back to close*[0.95, 1.05] for the
    # affected windows instead of emitting NaN or inf.
    nan_bars = _walk(60, seed=5)
    nan_bars.loc[nan_bars.index[35], "High"] = np.nan
    nan_bars.loc[nan_bars.index[36], "Low"] = np.nan
    for arr in compute_rolling_poc_series(nan_bars, lookback=20):
        assert np.isfinite(arr[19:]).all(), f"NaN input leaked into the output: {arr}"
        assert not np.isnan(arr[40:]).any()


def _curvature_reference(price, window, min_periods):
    """Independent least-squares reference (`np.linalg.lstsq` on an explicit
    Vandermonde design matrix, rather than `np.polyfit`'s SVD auto-solve)."""
    p = np.asarray(price, dtype=float).ravel()
    out = np.full(len(p), np.nan)
    for i in range(min_periods - 1, len(p)):
        seg = p[max(0, i - window + 1) : i + 1]
        seg = seg[~np.isnan(seg)]
        k = len(seg)
        if k < min_periods:
            continue
        sd = float(np.std(seg))
        if sd < 1e-8:
            out[i] = 0.0
            continue
        y = (seg - seg.mean()) / (sd + 1e-8)
        tau = np.linspace(-(k - 1) / 2.0, (k - 1) / 2.0, k)
        design = np.column_stack([tau**2, tau, np.ones(k)])
        a, b, _ = np.linalg.lstsq(design, y, rcond=None)[0]
        out[i] = (2.0 * a) / ((1.0 + b**2) ** 1.5)
    return out


def test_extremum_curvature_matches_lstsq_reference_on_series_and_dataframe():
    """M1: scale-free curvature agrees with an independent LLS reference, is
    invariant to price scaling, and accepts a single-column DataFrame as well
    as a Series (the two entry shapes used across the Track C strategies)."""
    bars = _walk(200, seed=6)
    close = bars["Close"]
    got = extremum_curvature(close, window=5, min_periods=3)
    ref = _curvature_reference(close.to_numpy(), 5, 3)
    np.testing.assert_allclose(got.to_numpy(), ref, atol=1e-10, equal_nan=True)
    assert list(got.index) == list(close.index) and got.name == "extremum_curvature"

    scaled = extremum_curvature(close * 500.0, window=5, min_periods=3)
    np.testing.assert_allclose(got.dropna(), scaled.dropna(), rtol=1e-5, atol=1e-5)

    as_frame = extremum_curvature(close.to_frame("Close"), window=5, min_periods=3)
    np.testing.assert_allclose(as_frame.to_numpy(), got.to_numpy(), atol=1e-12, equal_nan=True)


def test_extremum_curvature_boundary_inputs_and_nan_locality():
    """Empty / single / two-point / constant / all-NaN / NaN-injected inputs:
    correct shape, no exception, constant price maps to exactly 0.0 (not NaN),
    and one interior NaN perturbs at most `window` outputs (locality, i.e. no
    NaN diffusion)."""
    assert len(extremum_curvature(pd.Series(dtype=float))) == 0
    assert extremum_curvature(pd.Series([100.0])).isna().all()
    assert extremum_curvature(pd.Series([100.0, 101.0])).isna().all()
    assert extremum_curvature(pd.Series([np.nan] * 8)).isna().all()

    flat = extremum_curvature(pd.Series([100.0] * 12), window=5, min_periods=3)
    assert flat.iloc[:2].isna().all()
    assert np.all(flat.iloc[2:].to_numpy() == 0.0)

    priced = _walk(80, seed=7)["Close"].reset_index(drop=True)
    clean = extremum_curvature(priced, window=5, min_periods=3).to_numpy()
    poisoned = priced.copy()
    poisoned.iloc[40] = np.nan
    dirty = extremum_curvature(poisoned, window=5, min_periods=3).to_numpy()

    touched = np.flatnonzero(~np.isclose(clean, dirty, atol=1e-12, equal_nan=True))
    assert len(touched) > 0, "injecting a NaN must be observable"
    assert touched.max() <= 44, f"NaN spread beyond its own window: indices {touched}"
    assert np.isfinite(dirty[45:]).all(), "NaN must not poison everything after the window"


def test_extremum_curvature_window5_reachability_bound():
    """Mathematical property of M1's z-scored quadratic fit (this is what makes
    the 1.5 entry gates in C2.2/C3.1/C3.2 unreachable -- reported, not fixed):

    with 5 valid points (tau = -2..2) the residualised tau^2 basis has squared
    norm 14 while the z-scored y has squared norm 5, so |a| <= sqrt(5/14) and
    hence |kappa| <= 2*sqrt(5/14) ~= 1.1952 < 1.5 whenever the window is full.
    kappa >= 1.5 is only attainable when the window holds <= 4 valid points
    (i.e. only in the first 4 bars of a series, or across NaNs).
    """
    bound = 2.0 * np.sqrt(5.0 / 14.0)
    for seed in (11, 12, 13):
        for vol in (0.004, 0.012, 0.03):
            rs = np.random.RandomState(seed)
            price = pd.Series(100.0 * np.exp(np.cumsum(rs.normal(0.0, vol, 4000))))
            curv = extremum_curvature(price, window=5, min_periods=3).dropna()
            # Skip the first 4 bars: only there can k < 5 (min_periods=3).
            full_window = curv.iloc[4:]
            assert full_window.abs().max() <= bound + 1e-9, (
                f"full-window curvature {full_window.abs().max()} exceeded the {bound} bound"
            )
            assert not (full_window >= 1.5).any()


def test_standardized_divergence_matches_numpy_reference():
    """M2: the /sqrt(2) z-score divergent momentum agrees with an independent
    numpy rolling-std (ddof=1) reconstruction and stays dimensionless."""
    bars = _walk(150, seed=8)
    price = bars["Close"]
    ind = price.ewm(span=12, adjust=False).mean() - price.ewm(span=26, adjust=False).mean()
    lag, look = 5, 20
    got = standardized_divergence(price, ind, lag=lag, vol_lookback=look)

    ret = price.pct_change(lag).to_numpy()
    dind = ind.diff(lag).to_numpy()
    min_p = max(3, look // 2)

    def rolling_std(x, w, mp):
        out = np.full(len(x), np.nan)
        for i in range(len(x)):
            seg = x[max(0, i - w + 1) : i + 1]
            seg = seg[~np.isnan(seg)]
            if len(seg) >= mp:
                out[i] = seg.std(ddof=1)
        return out

    ref = (dind / (rolling_std(dind, look, min_p) + 1e-8) - ret / (rolling_std(ret, look, min_p) + 1e-8)) / np.sqrt(2.0)
    np.testing.assert_allclose(got.to_numpy(), ref, atol=1e-10, equal_nan=True)

    scaled = standardized_divergence(price, ind * 1e4, lag=lag, vol_lookback=look)
    np.testing.assert_allclose(got.dropna(), scaled.dropna(), rtol=1e-6, atol=1e-6)


def test_standardized_divergence_zero_variance_and_short_inputs():
    """Flat price/indicator must give 0.0 (0/eps) rather than inf/NaN, an empty
    input gives an empty output, and a short input is all-NaN inside its
    documented warmup -- never an exception."""
    flat = pd.Series(np.full(40, 100.0))
    div = standardized_divergence(flat, pd.Series(np.full(40, 1.0)), lag=5, vol_lookback=20)
    tail = div.dropna()
    assert len(tail) > 0 and np.all(np.isfinite(tail)) and np.all(tail == 0.0)

    assert len(standardized_divergence(pd.Series(dtype=float), pd.Series(dtype=float))) == 0
    short = standardized_divergence(pd.Series([101.0, 100.0, 102.0]), pd.Series([1.0, 0.9, 1.1]))
    assert len(short) == 3 and short.isna().all()


def _wilder_atr_reference(bars, period):
    """Explicit Wilder recursion (the module under test delegates to pandas'
    `ewm(alpha=1/period, adjust=False)`); `true_range[0] = High[0] - Low[0]`
    because pandas' row-wise `.max()` skips the two NaN comparison cells."""
    high = bars["High"].to_numpy(float)
    low = bars["Low"].to_numpy(float)
    close = bars["Close"].to_numpy(float)
    tr = np.empty(len(bars))
    tr[0] = high[0] - low[0]
    for i in range(1, len(bars)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    alpha = 1.0 / period
    out = np.full(len(bars), np.nan)
    prev = tr[0]
    for i in range(len(bars)):
        prev = tr[i] if i == 0 else (1 - alpha) * prev + alpha * tr[i]
        if i >= period - 1:
            out[i] = prev
    return out


def test_wyckoff_effort_result_matches_wilder_reference():
    """M3: effort-vs-result reproduces an explicit Wilder-ATR reconstruction,
    stays strictly positive (Effort > 0 by construction), and handles a missing
    /all-NaN Volume column identically."""
    bars = _walk(120, seed=9)
    lag, vlook = 5, 20
    got = wyckoff_effort_result(bars, lag=lag, volume_lookback=vlook)

    vol = bars["Volume"].to_numpy(float)
    vol_ma = pd.Series(vol).rolling(vlook, min_periods=1).mean().to_numpy()
    effort = vol / (vol_ma + 1e-8)
    disp = np.abs(bars["Close"].to_numpy(float) - bars["Close"].shift(lag).to_numpy(float))
    ref = (disp / (_wilder_atr_reference(bars, lag) + 1e-8)) / (effort + 1e-8)
    np.testing.assert_allclose(got.to_numpy(), ref, atol=1e-10, equal_nan=True)

    assert np.all(got.dropna() >= 0.0)
    signed = wyckoff_effort_result(bars, lag=lag, volume_lookback=vlook, signed=True)
    np.testing.assert_allclose(signed.abs().dropna(), got.dropna())

    no_vol = bars.drop(columns=["Volume"])
    all_nan_vol = bars.assign(Volume=np.nan)
    a = wyckoff_effort_result(no_vol, lag=lag, volume_lookback=vlook)
    b = wyckoff_effort_result(all_nan_vol, lag=lag, volume_lookback=vlook)
    np.testing.assert_allclose(a.to_numpy(), b.to_numpy(), atol=1e-12, equal_nan=True)


def test_wyckoff_effort_result_degenerate_inputs():
    """Constant price+volume -> exactly 0.0 (zero-division immunity); empty and
    single-bar frames -> correctly shaped, non-raising output; all-NaN OHLC ->
    NaN only where the input is NaN (no inf)."""
    flat = pd.DataFrame(
        {"High": np.full(30, 10.0), "Low": np.full(30, 10.0), "Close": np.full(30, 10.0), "Volume": np.full(30, 1000.0)}
    )
    er = wyckoff_effort_result(flat)
    assert len(er) == 30 and np.all(er.dropna() == 0.0)

    assert len(wyckoff_effort_result(pd.DataFrame({"High": [], "Low": [], "Close": []}))) == 0
    one = wyckoff_effort_result(pd.DataFrame({"High": [11.0], "Low": [9.0], "Close": [10.0]}))
    assert len(one) == 1

    all_nan = pd.DataFrame(
        {"High": [np.nan] * 30, "Low": [np.nan] * 30, "Close": [np.nan] * 30, "Volume": [np.nan] * 30}
    )
    res = wyckoff_effort_result(all_nan)
    assert np.isnan(res.to_numpy()).all() and not np.isinf(res.to_numpy()).any()


def test_wyckoff_spring_boundary_inputs_and_strict_causality():
    """M4: the (causal) spring detector never emits NaN, is unaffected by future
    bars, tolerates an empty frame / single bar / NaN support series, and its
    relative-volume output is strictly positive where it fires."""
    assert len(wyckoff_spring(pd.DataFrame({"High": [], "Low": [], "Close": []}), pd.Series(dtype=float))) == 0

    one = pd.DataFrame({"High": [101.0], "Low": [99.0], "Close": [100.0]})
    single_out = wyckoff_spring(one, pd.Series([99.0]))
    assert len(single_out) == 1 and np.isfinite(single_out.iloc[0])

    bars = _walk(80, seed=10)
    support = bars["Low"].rolling(20, min_periods=1).min().shift(1).fillna(bars["Low"])
    base = wyckoff_spring(bars, support)

    # Causality: rewriting every bar after index 40 must not change output[:41].
    future_rewritten = bars.copy()
    future_rewritten.iloc[41:, future_rewritten.columns.get_loc("Low")] *= 0.90
    future_rewritten.iloc[41:, future_rewritten.columns.get_loc("Close")] *= 1.05
    rewritten = wyckoff_spring(future_rewritten, support)
    np.testing.assert_allclose(base.iloc[:41].to_numpy(), rewritten.iloc[:41].to_numpy(), atol=0.0)

    assert not base.isna().any()
    assert np.all(base.to_numpy() >= 0.0)
    fired = base[base > 0.0]
    assert len(fired) > 0 and np.all(fired > 0.0), "spring output must be a positive rel-volume"

    nan_support = wyckoff_spring(bars, pd.Series(np.nan, index=bars.index))
    assert not nan_support.isna().any()
    assert np.all(nan_support.to_numpy() == 0.0), "NaN support must mean 'no spring', never NaN output"


def test_consolidation_density_matches_row_loop_reference():
    """M5: the overlap-ratio volume density matches an independent per-row
    Python loop, stays inside [0, 1], and NaN pivots degrade to 0.0."""
    bars = _walk(90, seed=12)
    zd = bars["Low"].rolling(20, min_periods=1).min() * 0.99
    zg = bars["High"].rolling(20, min_periods=1).max() * 1.01
    window = 15
    got = consolidation_density(bars, zd, zg, window=window)

    ratio = []
    for h, l, a, b, c in zip(bars["High"], bars["Low"], zd, zg, bars["Close"]):
        if np.isnan(a) or np.isnan(b) or not (b > a):
            ratio.append(0.0)
        elif h <= l:
            ratio.append(1.0 if (a <= c <= b) else 0.0)
        else:
            inter = max(0.0, min(h, b) - max(l, a))
            ratio.append(float(np.clip(inter / (h - l + 1e-8), 0.0, 1.0)))
    ratio = np.asarray(ratio)
    num = pd.Series(ratio * bars["Volume"].to_numpy(float), index=bars.index).rolling(window, min_periods=1).sum()
    den = pd.Series(bars["Volume"].to_numpy(float), index=bars.index).rolling(window, min_periods=1).sum()
    np.testing.assert_allclose(got.to_numpy(), np.clip(num / (den + 1e-8), 0.0, 1.0).to_numpy(), atol=1e-12)
    assert np.all((got >= 0.0) & (got <= 1.0))

    nan_pivots = consolidation_density(bars, pd.Series(np.nan, index=bars.index), pd.Series(np.nan, index=bars.index))
    assert np.all(nan_pivots.to_numpy() == 0.0) and not nan_pivots.isna().any()

    empty = consolidation_density(
        pd.DataFrame({"High": [], "Low": [], "Close": []}), pd.Series(dtype=float), pd.Series(dtype=float)
    )
    assert len(empty) == 0


def test_fractal_dimension_bounds_warmup_and_reference():
    """M5-fractal: D = 2 - H re-derived from the same window, bounded in
    [1, 2], all-NaN before the documented `min_chunk_size * 4` warmup, and a
    constant-price series degrades to NaN (undefined H) without raising."""
    from common.hurst import hurst_exponent

    bars = _walk(220, seed=13)
    close = bars["Close"]
    d = fractal_dimension(close, window=60, min_chunk_size=8)
    assert d.iloc[:32].isna().all()
    assert not d.iloc[32:].isna().any(), "no interior NaN once the window is full"
    assert np.all((d.dropna() >= 1.0) & (d.dropna() <= 2.0))

    log_ret = np.log(close / close.shift(1))
    t = 150
    h = hurst_exponent(log_ret.iloc[t - 59 : t + 1].dropna(), min_chunk_size=8)
    np.testing.assert_allclose(d.iloc[t], float(np.clip(2.0 - h, 1.0, 2.0)), atol=1e-12)

    assert len(fractal_dimension(pd.Series(dtype=float))) == 0
    assert fractal_dimension(pd.Series(np.arange(10.0))).isna().all()
    const = fractal_dimension(pd.Series(np.full(80, 100.0)))
    assert len(const) == 80 and const.isna().all(), "zero-variance log returns => undefined H => NaN, not 0.0"

    # NOTE (C4.1): unlike `extremum_curvature`/`standardized_divergence`, this
    # operator is Series-only -- passing a single-column DataFrame raises
    # pandas' opaque "Data must be 1-dimensional" ValueError out of
    # `hurst_exponent`. Every call site feeds `df["Close"]`, so this is an API
    # inconsistency, not a functional defect; reported, not asserted against.


def test_enrich_single_symbol_matches_panel_no_cross_asset_contamination():
    """The enrichment pipeline must be per-symbol: a symbol's structural
    features are identical whether it is enriched alone (single-series input)
    or inside a multi-symbol DataFrame panel -- no cross-asset NaN diffusion,
    no index misalignment."""
    a = _walk(200, seed=21)
    b = _walk(200, seed=22)
    b.index = a.index
    panel = enrich_universe_with_structural_features({"AAA": a, "BBB": b})
    solo = enrich_universe_with_structural_features({"AAA": a})

    for col in ("POC", "VAL", "VAH", "extremum_curvature", "standardized_divergence",
                "wyckoff_effort_result", "consolidation_density", "cyq_winner_ratio",
                "cyq_cost_concentration", "wyckoff_spring", "fractal_dimension"):
        assert col in panel["AAA"].columns and col in solo["AAA"].columns
        np.testing.assert_allclose(
            panel["AAA"][col].to_numpy(), solo["AAA"][col].to_numpy(), atol=1e-12, equal_nan=True,
            err_msg=f"panel={col} differs from single-symbol enrichment",
        )
        assert np.isinf(panel["BBB"][col].to_numpy()).sum() == 0

    # Input frames are not mutated (inplace=False default).
    assert "POC" not in a.columns


def test_cyq_degenerate_universe_stays_bounded():
    """C1.3: CYQ features remain in [0, 1] on a constant-price symbol, a
    single-bar frame, and an all-NaN-Volume frame (no inf/NaN blow-up)."""
    const = pd.DataFrame(
        {"High": np.full(40, 50.0), "Low": np.full(40, 50.0), "Close": np.full(40, 50.0), "Volume": np.full(40, 1.0)}
    )
    wr, cc = cyq_chip_distribution(const)
    assert np.all((wr >= 0.0) & (wr <= 1.0)) and np.all((cc >= 0.0) & (cc <= 1.0))

    wr1, cc1 = cyq_chip_distribution(const.iloc[:1])
    assert len(wr1) == 1 and len(cc1) == 1 and np.isfinite(wr1.iloc[0])

    no_vol = const.assign(Volume=np.nan)
    wr2, cc2 = cyq_chip_distribution(no_vol)
    assert np.all(np.isfinite(wr2.to_numpy())) and np.all((wr2 >= 0.0) & (wr2 <= 1.0))

    empty = cyq_chip_distribution(const.iloc[:0])
    assert len(empty[0]) == 0 and len(empty[1]) == 0


# --------------------------------------------------------------------------- #
# 2. Sparse-weights contract for the two Track C strategies
# --------------------------------------------------------------------------- #
def test_chan_vp_hybrid_real_path_sparse_contract():
    """The real (unpatched) `generate_weights` output obeys the contract on
    every code path it can reach."""
    universe = _two_symbol_universe(300)
    weights = ChanVolumeProfileHybridStrategy().generate_weights(universe)
    assert set(universe).issubset(set(weights.columns))
    _assert_sparse_contract(weights, universe["SPY"].index)
    assert weights.loc[weights.index[0], "BIL"] == pytest.approx(1.0)
    assert weights.loc[weights.index[0], "SPY"] == 0.0


def test_wyckoff_spring_real_path_sparse_contract():
    universe = _two_symbol_universe(300)
    weights = WyckoffChanSpringStrategy().generate_weights(universe)
    assert set(universe).issubset(set(weights.columns))
    _assert_sparse_contract(weights, universe["SPY"].index)
    assert weights.loc[weights.index[0], "BIL"] == pytest.approx(1.0)
    assert weights.loc[weights.index[0], "SPY"] == 0.0


def _drive_scripted(strategy_module, loop_name, universe, scripted, monkeypatch_ctx):
    """Run `generate_weights` with the strategy's own stateful position loop
    replaced by a scripted weight path -- the documented seam, so the sparse
    assembly (`_cap_and_deroute_to_cash` -> `_fill_out_columns` ->
    `_sparse_from_daily`) is exercised with real entry/scale/exit transitions
    that generic synthetic prices do not produce."""
    state = {"calls": 0}

    def fake_loop(**kwargs):
        """Only the FIRST risky symbol gets the scripted path; every other
        symbol stays flat (otherwise the `cap = 1.0` scaling in
        `_cap_and_deroute_to_cash` would halve the scripted path instead of
        leaving one unambiguous position to assert on)."""
        state["calls"] += 1
        n = len(kwargs["close"])
        if state["calls"] == 1:
            return np.asarray(scripted[:n], dtype=float)
        return np.zeros(n, dtype=float)

    monkeypatch_ctx.setattr(strategy_module, loop_name, fake_loop)
    return strategy_module


def test_chan_vp_hybrid_scripted_transitions_write_explicit_zero_on_exit(monkeypatch):
    universe = {"SPY": _walk(300, seed=31), "QQQ": _walk(300, seed=32), "BIL": _walk(300, seed=33)}
    for df in universe.values():
        df.index = universe["SPY"].index
    idx = universe["SPY"].index
    scripted = _scripted_path(idx, [(80, 1.0), (120, 0.5), (160, 0.0)])
    _drive_scripted(cvp_mod, "run_chan_vp_position_loop", universe, scripted, monkeypatch)

    weights = ChanVolumeProfileHybridStrategy().generate_weights(universe)
    n_rows = _assert_sparse_contract(weights, idx)

    assert n_rows == 4, f"expected 4 transition rows (initial/entry/scale/exit), got {n_rows}"
    assert weights.loc[idx[0], "SPY"] == 0.0 and weights.loc[idx[0], "BIL"] == pytest.approx(1.0)
    assert weights.loc[idx[80], "SPY"] == pytest.approx(1.0)
    assert weights.loc[idx[120], "SPY"] == pytest.approx(0.5)
    # The de-risked full exit must be an EXPLICIT 0.0 -- a NaN there would make
    # the ledger's column-wise ffill keep the stale 0.5 position forever.
    assert weights.loc[idx[160], "SPY"] == 0.0
    assert weights.loc[idx[160], "BIL"] == pytest.approx(1.0)
    # De-risked-but-never-held symbol is also explicit 0.0 on every rebalance row.
    assert (weights.dropna(how="all")["QQQ"] == 0.0).all()
    # Non-transition bars carry no instruction at all.
    assert weights.iloc[81:120].isna().all().all()


def test_wyckoff_spring_scripted_transitions_write_explicit_zero_on_exit(monkeypatch):
    universe = {"SPY": _walk(300, seed=41), "QQQ": _walk(300, seed=42), "BIL": _walk(300, seed=43)}
    for df in universe.values():
        df.index = universe["SPY"].index
    idx = universe["SPY"].index
    scripted = _scripted_path(idx, [(80, 1.0), (120, 0.5), (160, 0.0)])
    _drive_scripted(wyc_mod, "run_wyckoff_spring_position_loop", universe, scripted, monkeypatch)

    weights = WyckoffChanSpringStrategy().generate_weights(universe)
    n_rows = _assert_sparse_contract(weights, idx)

    assert n_rows == 4
    assert weights.loc[idx[80], "SPY"] == pytest.approx(1.0)
    assert weights.loc[idx[120], "SPY"] == pytest.approx(0.5)
    assert weights.loc[idx[160], "SPY"] == 0.0
    assert weights.loc[idx[160], "BIL"] == pytest.approx(1.0)
    assert (weights.dropna(how="all")["QQQ"] == 0.0).all()
    assert weights.iloc[81:120].isna().all().all()


@pytest.mark.parametrize("strategy_cls", [ChanVolumeProfileHybridStrategy, WyckoffChanSpringStrategy])
def test_track_c_strategies_never_forward_fill_their_own_output(strategy_cls):
    """`AGENTS.md`: a template must never ffill its own output. Mechanically:
    the sparse frame differs from its own ffill, and its non-NaN dates are
    exactly the dates on which the reconstructed dense target changes."""
    universe = _two_symbol_universe(260)
    weights = strategy_cls().generate_weights(universe)
    assert not weights.ffill().equals(weights), "frame looks forward-filled"

    dense = weights.dropna(how="all").reindex(weights.index).ffill().fillna(0.0)
    changed = dense.ne(dense.shift(1)).any(axis=1)
    changed.iloc[0] = True
    assert set(weights.dropna(how="all").index) == set(dense.index[changed])


@pytest.mark.parametrize("strategy_cls", [ChanVolumeProfileHybridStrategy, WyckoffChanSpringStrategy])
def test_track_c_strategies_handle_degenerate_universes(strategy_cls):
    """Empty universe -> empty frame; short history -> no entry, no NaN; a
    single tradable symbol with no cash proxy still yields a valid frame."""
    assert strategy_cls().generate_weights({}).empty

    short = {"SPY": make_ohlcv_from_closes(np.linspace(100.0, 108.0, 15))}
    weights = strategy_cls().generate_weights(short)
    assert list(weights.columns) == ["SPY"]
    assert not weights.isna().all(axis=1).all() or weights.empty
    assert np.isfinite(weights.fillna(0.0).to_numpy()).all()


# --------------------------------------------------------------------------- #
# 3. B1.1 ledger semantics + all-strategy regression sweep
# --------------------------------------------------------------------------- #
def test_ledger_semantics_actual_weight_never_overfills(monkeypatch):
    """B1.1 re-baseline: `actual_weights` is `position * P / equity`, so with
    integer lots it can sit slightly BELOW the target on the purchase bar -- it
    is never allowed to exceed it however (no margin / no negative cash), and a
    full exit must land on exactly 0.0. This is the assertion shape that
    replaces the pre-B1.1 "actual_w == target" / "cash == 0 when fully
    invested" checks, both of which the ledger engine deliberately abolished.
    """
    universe = {"SPY": _walk(200, seed=51), "BIL": _walk(200, seed=52)}
    for df in universe.values():
        df.index = universe["SPY"].index
    idx = universe["SPY"].index
    scripted = _scripted_path(idx, [(60, 1.0), (120, 0.0)])
    _drive_scripted(wyc_mod, "run_wyckoff_spring_position_loop", universe, scripted, monkeypatch)
    weights = WyckoffChanSpringStrategy().generate_weights(universe)

    res = run_allocation_backtest(universe=universe, target_weights=weights, initial_capital=100_000.0, min_shares=1)
    actual = res["actual_weights"]
    report = res["rebalance_report"]

    assert np.isfinite(res["equity_curve"]["equity"]).all()
    assert (res["equity_curve"]["equity"] > 0.0).all()
    assert np.isfinite(actual.to_numpy()).all()
    assert np.all(actual.to_numpy() >= -1e-12) and np.all(actual.to_numpy() <= 1.0 + 1e-9)

    buys = report[report["action"] == "BUY"]
    sells = report[report["action"] == "SELL"]
    assert len(buys) > 0 and len(sells) > 0, "both an entry and an exit must actually trade"
    assert np.all(buys["shares"] > 0) and np.all(sells["shares"] > 0)

    # Long-only, self-financed: SPY's actual weight can never exceed its target
    # beyond price drift within one bar, and BIL+SPY stays inside [0, 1].
    tot = actual.sum(axis=1)
    assert (tot <= 1.0 + 2e-2).all(), f"portfolio weights over-allocated: max={tot.max()}"
    # The instructed full exit lands exactly flat (a lot-rounded residual is the
    # engine's job to sell, not something to tolerate).
    assert actual.loc[idx[120]:, "SPY"].max() == pytest.approx(0.0, abs=1e-12)


def test_ledger_lot_rounding_means_actual_weight_is_not_the_target(monkeypatch):
    """Explicitly documents the B1.1 semantic that broke pre-existing style
    assertions: with `min_shares=100`, a 1/3 target cannot be hit exactly, so
    `actual_w == target` is false by construction while `actual_w -> target`
    still holds to within one lot's notional."""
    universe = {"SPY": _walk(120, seed=61), "BIL": _walk(120, seed=62)}
    for df in universe.values():
        df.index = universe["SPY"].index
    idx = universe["SPY"].index
    scripted = _scripted_path(idx, [(40, 0.3333)])
    _drive_scripted(wyc_mod, "run_wyckoff_spring_position_loop", universe, scripted, monkeypatch)
    weights = WyckoffChanSpringStrategy().generate_weights(universe)

    res = run_allocation_backtest(universe=universe, target_weights=weights, min_shares=100)
    actual = res["actual_weights"]
    traded = res["rebalance_report"]
    assert len(traded) > 0

    entry = idx[40]
    spy_actual = float(actual.loc[entry, "SPY"])
    spy_target = float(weights.loc[entry, "SPY"])
    assert spy_actual <= spy_target + 1e-2
    lot_notional = 100 * float(universe["SPY"].loc[entry, "Close"])
    equity = float(res["equity_curve"].loc[entry, "equity"])
    assert abs(spy_actual - spy_target) <= 2.0 * lot_notional / equity + 1e-6, (
        "actual weight must track the target to within whole-lot granularity"
    )


_STATIC_TEMPLATES = [
    EqualWeightAllocation,
    InverseVolatilityAllocation,
    CrossSectionalMomentumAllocation,
    HierarchicalRiskParityAllocation,
    DualMomentumAllocation,
    MaxDiversificationAllocation,
    MeanReversionAllocation,
    MinimumVarianceAllocation,
    BreadthGatedMomentumAllocation,
]


def test_all_registered_strategies_produce_contract_compliant_sparse_weights():
    """The "55+ strategies still work" regression, as an assertion: every
    registered strategy class plus all 9 static allocation templates must run
    `generate_weights` offline and return a contract-compliant sparse frame.

    `NaturalLanguageStrategy` is the single documented exception -- it cannot be
    constructed without a parsed spec / NL text, so it is exercised through the
    config-driven path (`instantiate_strategy_from_config_entry`) instead.
    """
    n = 260
    symbols = ["SPY", "QQQ", "TLT", "GLD", "IEF", "BIL", "DBC", "VNQ"]
    universe = {}
    for i, sym in enumerate(symbols):
        df = make_trending_pullback_df(n=n, seed=7 + i, start="2018-01-01")
        df["Volume"] = 1.0e6 + np.random.default_rng(i).uniform(0.0, 1.0e6, n)
        universe[sym] = df
    index = next(iter(universe.values())).index

    skipped = set()
    checked = 0
    for name, cls in STRATEGY_CLASS_MAP.items():
        try:
            strategy = cls()
        except TypeError:
            skipped.add(name)
            continue
        weights = strategy.generate_weights(universe)
        assert isinstance(weights, pd.DataFrame), f"{name} must return a DataFrame"
        if weights.empty:
            continue
        _assert_sparse_contract(weights, index)
        checked += 1

    assert skipped == {"NaturalLanguageStrategy"}, f"unexpected un-instantiable strategies: {skipped}"
    assert checked >= 45, f"only {checked} strategies were contract-checked"

    template_checked = 0
    for tpl_cls in _STATIC_TEMPLATES:
        tpl = tpl_cls()
        # Static templates read their params dict positionally (no defaults), so
        # seed every declared grid key with its first candidate value.
        params = {k: v[0] for k, v in (tpl.param_grid or {}).items()}
        params.setdefault("rebalance_freq_days", 21)
        params.setdefault("vol_lookback", 20)
        try:
            weights = tpl.generate_weights(universe, params)
        except IndexError:
            # Documented pre-existing issue (AGENTS.md "Known pre-existing
            # issues"): HRP's quasi-diagonal recursion can index past a
            # degenerate cluster tree on synthetic universes. In production
            # `_portfolio_score` catches it and degrades the candidate to -inf
            # Sharpe, so this is not a Track C regression -- but it must stay
            # confined to that one template.
            assert tpl_cls is HierarchicalRiskParityAllocation, (
                f"unexpected IndexError from {tpl_cls.__name__}"
            )
            continue
        assert isinstance(weights, pd.DataFrame)
        if weights.empty:
            continue
        _assert_sparse_contract(weights, index)
        assert isinstance(tpl.explain_weights(params), str)
        assert tpl.warmup_bars(params) >= 0
        template_checked += 1
    assert template_checked >= 7


# --------------------------------------------------------------------------- #
# 4. Track C gate-reachability ledger
#    C5.1/C5.2 changed the status of both entries below: the Wyckoff curvature gate
#    was fixed in production (1.5 -> 0.8) and the Chan VP HVN gate was fixed in
#    production, but this fixture still lacks a V-turn, so only the HVN test can be
#    un-xfailed here. The surviving xfail is a FIXTURE limitation, documented inline.
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    strict=True,
    reason=(
        "FIXTURE limitation, not a production defect (re-verified empirically after "
        "Task C5.1 2026-10-23). The `_two_symbol_universe(400)` random-walk fixture has "
        "no V-turn: `wyckoff_spring >= 1` only on 2 bars (2019-01-02, 2019-07-09) and "
        "kappa < 0.8 on exactly those 2 bars (kappa ~= 0.0513 and ~= 0.2043), so this "
        "test cannot exercise the curvature gate at all -- no threshold at or above 0.8 "
        "can fire here. Curvature-gate reachability is instead proven by "
        "test_wyckoff_chan_strategy.py::"
        "test_wyckoff_chan_spring_reachable_curvature_gate_opens_position, where a "
        "genuine Wyckoff Spring on make_spring_universe() carries kappa ~= 1.0101 and "
        "provably opens a position at the new 0.8 default (and provably not at the "
        "legacy unreachable 1.5). Do NOT remove this xfail -- or weaken/repoint the "
        "assertion to the fixture's low-curvature springs -- until "
        "`_two_symbol_universe` itself gains a genuine V-turn."
    ),
)
def test_wyckoff_curvature_entry_gate_is_reachable():
    """Intent of C2.2: a spring confirmed by the curvature gate should open a position.

    (C2.2 stated the gate at the then-default 1.5; Task C5.1 lowered the default to
    0.8 because 1.5 exceeds the analytic window-5 bound.) Still xfailed for a FIXTURE
    reason (see the marker above): this random walk has no V-turn at all, so the gate
    is unreachable on it regardless of which of those two thresholds is used.
    """
    universe = _two_symbol_universe(400)
    weights = WyckoffChanSpringStrategy().generate_weights(universe)
    dense = weights.dropna(how="all").reindex(weights.index).ffill().fillna(0.0)
    assert (dense["SPY"] > 0.0).any(), "the spring + curvature gate never opened a position"


# Task C5.2 retired this marker: the HVN gate was DEAD, not merely unreachable on
# synthetic data -- `consolidation_density` was measured against `[ZD, ZG]` from the
# already-confirmed Chan 中枢, which by construction lags a stroke/segment behind the
# price, so it had exactly zero overlap with the same-window 60d POC and
# hvn_instant/hvn_valid were identically False. Fixed by feeding both the density
# inputs and the POC interval the same-window `VAL/VAH` of the rolling profile
# (`[ZD, ZG]` still drive breakout/retest/stop levels). This test now guards against
# the gate silently going dead again -- it must stay live (no xfail, no skip).
def test_chan_vp_hvn_gate_is_reachable():
    universe = _two_symbol_universe(400)
    weights = ChanVolumeProfileHybridStrategy().generate_weights(universe)
    dense = weights.dropna(how="all").reindex(weights.index).ffill().fillna(0.0)
    assert (dense["SPY"] > 0.0).any(), "the HVN-validated pivot gate never opened a position"

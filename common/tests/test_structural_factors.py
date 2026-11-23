"""Unit tests for common/structural_factors.py feature enrichment pipeline.

Validates Task B2.1:
1. enrich_universe_with_structural_features creates all 12 required columns.
2. Idempotent memoization skips computation when columns already exist.
3. Inplace vs copy behavior.
4. Robustness against empty and short DataFrames.

100% offline using synthetic OHLCV data generators.
"""

import itertools
import math
import os
import sys
import warnings
import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.structural_factors import (
    CURVATURE_KAPPA_BOUND_WINDOW5,
    DEFAULT_REACHABLE_MIN_CURVATURE,
    enrich_universe_with_structural_features,
    compute_rolling_poc_series,
    extremum_curvature,
    fractal_dimension,
    standardized_divergence,
    warn_unreachable_curvature_threshold,
)
from common.testing import make_ohlcv_from_closes


REQUIRED_COLUMNS = [
    "cyq_winner_ratio",
    "cyq_cost_concentration",
    "wyckoff_spring",
    "extremum_curvature",
    "standardized_divergence",
    "wyckoff_effort_result",
    "fractal_dimension",
    "consolidation_density",
    "POC",
    "VAL",
    "VAH",
    "NPOC",
]


def _build_test_bars(n: int = 80, base: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    closes = base + np.cumsum(np.random.normal(0, 0.5, n))
    bars = make_ohlcv_from_closes(closes)
    bars.index = dates
    bars["Volume"] = 10000.0
    bars["Turn"] = 2.0
    return bars


def test_enrich_universe_with_structural_features_creates_all_columns():
    """Verify all 12 required structural columns are attached to universe DataFrames."""
    np.random.seed(42)
    universe = {
        "ASSET_A": _build_test_bars(80, 10.0),
        "ASSET_B": _build_test_bars(80, 50.0),
    }

    enriched = enrich_universe_with_structural_features(universe, lookback=60, inplace=False)

    for sym, df in enriched.items():
        assert len(df) == 80
        for col in REQUIRED_COLUMNS:
            assert col in df.columns, f"Missing required column {col} in {sym}"
            assert len(df[col]) == 80

        # Check values
        assert not df["POC"].dropna().empty
        assert not df["VAL"].dropna().empty
        assert not df["VAH"].dropna().empty
        assert not df["NPOC"].dropna().empty
        assert np.all((df["cyq_winner_ratio"] >= 0.0) & (df["cyq_winner_ratio"] <= 1.0))
        assert np.all((df["cyq_cost_concentration"] >= 0.0) & (df["cyq_cost_concentration"] <= 1.0))


def test_enrich_universe_with_structural_features_memoization_idempotency():
    """Verify pre-existing columns are skipped and not overwritten."""
    np.random.seed(42)
    bars = _build_test_bars(80, 20.0)
    for col in REQUIRED_COLUMNS:
        bars[col] = 999.0

    universe = {"MEMO_ASSET": bars}
    enriched = enrich_universe_with_structural_features(universe, lookback=60, inplace=False)

    # Should have skipped re-computation and preserved 999.0
    for col in REQUIRED_COLUMNS:
        assert np.all(enriched["MEMO_ASSET"][col] == 999.0)


def test_enrich_universe_with_structural_features_inplace_flag():
    """Verify inplace=True modifies original frame, while inplace=False preserves it."""
    np.random.seed(42)
    bars_inplace = _build_test_bars(80, 30.0)
    universe_inplace = {"ASSET_IP": bars_inplace}
    res_inplace = enrich_universe_with_structural_features(universe_inplace, lookback=60, inplace=True)
    assert res_inplace["ASSET_IP"] is bars_inplace
    assert "POC" in bars_inplace.columns

    bars_copy = _build_test_bars(80, 40.0)
    universe_copy = {"ASSET_CP": bars_copy}
    res_copy = enrich_universe_with_structural_features(universe_copy, lookback=60, inplace=False)
    assert res_copy["ASSET_CP"] is not bars_copy
    assert "POC" not in bars_copy.columns
    assert "POC" in res_copy["ASSET_CP"].columns


def test_enrich_universe_with_structural_features_empty_and_short_df():
    """Verify empty and short dataframes are handled safely without exceptions."""
    empty_df = pd.DataFrame()
    short_df = pd.DataFrame({
        "High": [10.0, 10.5],
        "Low": [9.5, 9.8],
        "Close": [9.9, 10.2],
        "Volume": [100.0, 150.0],
    })
    universe = {"EMPTY": empty_df, "SHORT": short_df}
    enriched = enrich_universe_with_structural_features(universe, lookback=60, inplace=False)
    assert enriched["EMPTY"].empty
    assert len(enriched["SHORT"]) == 2
    for col in REQUIRED_COLUMNS:
        assert col in enriched["SHORT"].columns


# ---------------------------------------------------------------------------
# Task C5.3: the three Series-level operators must share ONE input contract.
# ---------------------------------------------------------------------------

_SERIES_LEVEL_OPERATORS = ("extremum_curvature", "standardized_divergence", "fractal_dimension")


def _call_series_operator(op_name, price, indicator):
    if op_name == "extremum_curvature":
        return extremum_curvature(price)
    if op_name == "fractal_dimension":
        return fractal_dimension(price)
    return standardized_divergence(price, indicator)


@pytest.mark.parametrize("op_name", _SERIES_LEVEL_OPERATORS)
def test_series_level_operators_accept_series_and_single_column_frame(op_name):
    """Series / 1-column DataFrame must produce identical 1-D output.

    Regression for C5.3: fractal_dimension used to raise
    "Data must be 1-dimensional" on a 1-column frame (its R/S estimator got an
    (n,1) array), extremum_curvature silently worked, and
    standardized_divergence returned a DataFrame instead of a Series.
    """
    rs = np.random.RandomState(0)
    close = pd.Series(100.0 * np.exp(np.cumsum(rs.normal(0.0, 0.01, 160))))
    indicator = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()

    as_series = _call_series_operator(op_name, close, indicator)
    as_frame = _call_series_operator(op_name, close.to_frame("Close"), indicator)

    assert isinstance(as_series, pd.Series), f"{op_name} must return a Series"
    assert isinstance(as_frame, pd.Series), f"{op_name} must return a Series for 1-D input"
    assert list(as_frame.index) == list(close.index)
    np.testing.assert_allclose(
        as_frame.to_numpy(), as_series.to_numpy(), rtol=0.0, atol=1e-12, equal_nan=True
    )


def test_standardized_divergence_accepts_single_column_indicator_frame():
    """The indicator argument follows the same 1-D contract (C5.3)."""
    rs = np.random.RandomState(1)
    close = pd.Series(100.0 * np.exp(np.cumsum(rs.normal(0.0, 0.01, 120))))
    ind = pd.Series(rs.normal(0.0, 1.0, 120)).cumsum()
    ref = standardized_divergence(close, ind)
    got = standardized_divergence(close, ind.to_frame("ind"))
    assert isinstance(got, pd.Series)
    np.testing.assert_allclose(got.to_numpy(), ref.to_numpy(), atol=1e-12, equal_nan=True)


@pytest.mark.parametrize("op_name", _SERIES_LEVEL_OPERATORS)
def test_series_level_operators_reject_multi_column_frame(op_name):
    """A multi-column frame is ambiguous and must raise explicitly (C5.3)."""
    close = pd.Series(np.linspace(100.0, 110.0, 40))
    indicator = pd.Series(np.linspace(0.0, 1.0, 40))
    multi = pd.DataFrame({"A": close, "B": close * 1.01})
    with pytest.raises(
        ValueError,
        match=(
            rf"{op_name} expects a Series or a single-column DataFrame; "
            rf"got a DataFrame with 2 columns"
        ),
    ):
        _call_series_operator(op_name, multi, indicator)


# ---------------------------------------------------------------------------
# Task C5.1: curvature reachability (the 1.5 gate was structurally dead).
# ---------------------------------------------------------------------------


def test_curvature_kappa_bound_constant_matches_algebra():
    assert math.isclose(
        CURVATURE_KAPPA_BOUND_WINDOW5, 2.0 * math.sqrt(5.0 / 14.0), rel_tol=0.0, abs_tol=1e-15
    )
    assert math.isclose(CURVATURE_KAPPA_BOUND_WINDOW5, 1.1952286093343936, rel_tol=0.0, abs_tol=1e-12)


def test_extremum_curvature_window5_bound_respected_adversarially():
    """|kappa| <= 2*sqrt(5/14) for every FULL window=5 fit.

    Adversarial coverage: the complete {-1,0,1}^5 sign/level grid plus ~54k
    random windows at several volatility scales. min_periods=5 keeps partial
    warm-up fits out (their k=3/k=4 bounds are 4.2426 and 2.0, which is exactly
    why the historical 1.5 gate looked alive in the first bars of a series).
    """
    bound = CURVATURE_KAPPA_BOUND_WINDOW5

    grid = np.array(list(itertools.product([-1.0, 0.0, 1.0], repeat=5)))
    for window in grid:
        kappa = extremum_curvature(pd.Series(window), window=5, min_periods=5).iloc[-1]
        assert abs(kappa) <= bound + 1e-12, f"grid window {window} gave kappa={kappa}"
        # The old production gate could never fire on a full window.
        assert kappa < 1.5

    rs = np.random.RandomState(12345)
    for vol in (0.002, 0.01, 0.05):
        for _ in range(3):
            price = pd.Series(100.0 * np.exp(np.cumsum(rs.normal(0.0, vol, 6000))))
            curv = extremum_curvature(price, window=5, min_periods=5).dropna()
            assert len(curv) > 0
            assert curv.abs().max() <= bound + 1e-9, (
                f"full-window |kappa| {curv.abs().max()} exceeded analytic bound {bound}"
            )
            assert not (curv.abs() >= 1.1953).any()


def test_reachable_default_curvature_attainable_by_v_bottom():
    """DEFAULT_REACHABLE_MIN_CURVATURE=0.8 must lie strictly inside the bound.

    A symmetric 5-point V-bottom [2,1,0,1,2] scores kappa ~= 1.1454, so the new
    default is genuinely attainable by a realistic reversal fixture while the
    old 1.5 was not. (0.8 is an INTERIM reachability default, not a
    walkforward-optimised threshold.)
    """
    assert 0.0 < DEFAULT_REACHABLE_MIN_CURVATURE < CURVATURE_KAPPA_BOUND_WINDOW5

    v_bottom = pd.Series([2.0, 1.0, 0.0, 1.0, 2.0])
    kappa = extremum_curvature(v_bottom, window=5, min_periods=5).iloc[-1]
    # Deterministic and scale-invariant: [2,1,0,1,2] and [1,0.5,0,0.5,1]
    # both score ~= 1.1454 (differing only at the 8th decimal from polyfit).
    assert math.isclose(kappa, 1.1454, rel_tol=0.0, abs_tol=1e-3)
    assert DEFAULT_REACHABLE_MIN_CURVATURE < kappa < 1.5

    # Zero-mean noise is centred on kappa ~= 0 (though individual windows can
    # approach the bound), so 0.8 still filters most non-turning-point windows.
    rs = np.random.RandomState(2024)
    noise_kappas = np.array([
        extremum_curvature(pd.Series(rs.normal(size=5)), window=5, min_periods=5).iloc[-1]
        for _ in range(4000)
    ])
    assert abs(noise_kappas.mean()) < 0.05


def test_warn_unreachable_curvature_threshold():
    """The dead-gate guard warns at/above the bound and stays silent below."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert warn_unreachable_curvature_threshold(DEFAULT_REACHABLE_MIN_CURVATURE) is False
        assert warn_unreachable_curvature_threshold(1.19) is False
        # Exactly at the analytic bound -> unreachable (a full window can only
        # touch it, never exceed it, so `>=` cannot fire in practice).
        assert warn_unreachable_curvature_threshold(CURVATURE_KAPPA_BOUND_WINDOW5) is True
        assert warn_unreachable_curvature_threshold(1.1953) is True
        assert warn_unreachable_curvature_threshold(1.5, context="some.gate") is True
        assert warn_unreachable_curvature_threshold(-1.5) is True

    assert len(caught) == 4
    assert all(issubclass(w.category, UserWarning) for w in caught)
    assert "some.gate" in str(caught[2].message)
    assert "1.5" in str(caught[2].message)


# =============================================================================
# MS2: CYQ Cost Deviation & 90-10 Quantile Spread
# =============================================================================


def test_ms2_cyq_chip_distribution_backward_compatible_and_directional_cost_dev():
    from common.structural_factors import (
        cyq_chip_distribution,
        cyq_cost_deviation,
        cyq_quantile_spread_90_10,
    )

    dates = pd.bdate_range("2025-01-02", periods=30)
    up_close = np.linspace(100.0, 160.0, 30)
    bars_up = pd.DataFrame(
        {
            "Open": up_close - 0.5,
            "High": up_close + 1.0,
            "Low": up_close - 1.0,
            "Close": up_close,
            "Volume": np.full(30, 1000.0),
        },
        index=dates,
    )
    dn_close = np.linspace(160.0, 100.0, 30)
    bars_dn = pd.DataFrame(
        {
            "Open": dn_close + 0.5,
            "High": dn_close + 1.0,
            "Low": dn_close - 1.0,
            "Close": dn_close,
            "Volume": np.full(30, 1000.0),
        },
        index=dates,
    )

    # 2-tuple backward compatibility
    res = cyq_chip_distribution(bars_up)
    assert isinstance(res, tuple) and len(res) == 2
    wr, cc = res
    assert wr.name == "cyq_winner_ratio"
    assert cc.name == "cyq_cost_concentration"

    # Directional sign of cost deviation
    dev_up = cyq_cost_deviation(bars_up)
    dev_dn = cyq_cost_deviation(bars_dn)
    assert dev_up.name == "cyq_cost_deviation"
    assert (dev_up.iloc[1:] > 0.0).all()
    assert (dev_dn.iloc[1:] < 0.0).all()

    # Quantile spread 90-10: non-negative and smaller on tight consolidation than volatile bars
    q_up = cyq_quantile_spread_90_10(bars_up)
    assert q_up.name == "cyq_quantile_spread_90_10"
    assert (q_up >= 0.0).all()

    bars_tight = pd.DataFrame(
        {
            "Open": np.full(30, 100.0),
            "High": np.full(30, 100.5),
            "Low": np.full(30, 99.5),
            "Close": np.full(30, 100.0),
            "Volume": np.full(30, 1000.0),
        },
        index=dates,
    )
    q_tight = cyq_quantile_spread_90_10(bars_tight)
    assert q_tight.iloc[-1] < q_up.iloc[-1]


def test_ms2_real_turn_priority_and_ms12_causality():
    from common.lookahead_checks import assert_future_append_invariant
    from common.structural_factors import (
        cyq_cost_deviation,
        cyq_quantile_spread_90_10,
    )

    dates = pd.bdate_range("2025-01-02", periods=30)
    up_close = np.linspace(100.0, 160.0, 30)
    base = pd.DataFrame(
        {
            "Open": up_close - 0.5,
            "High": up_close + 1.0,
            "Low": up_close - 1.0,
            "Close": up_close,
            "Volume": np.full(30, 1000.0),
        },
        index=dates,
    )

    high_turn = base.assign(Turn=50.0)
    low_turn = base.assign(Turn=1.5)

    dev_high = cyq_cost_deviation(high_turn, turn_mode="raw")
    dev_low = cyq_cost_deviation(low_turn, turn_mode="raw")
    dev_proxy = cyq_cost_deviation(base)
    q_high = cyq_quantile_spread_90_10(high_turn, turn_mode="raw")
    q_low = cyq_quantile_spread_90_10(low_turn, turn_mode="raw")

    # High turnover anchors mean holding cost much closer to today's close
    assert abs(dev_high.iloc[-1]) < abs(dev_low.iloc[-1])
    assert not np.allclose(dev_high.to_numpy(), dev_proxy.to_numpy())
    assert not np.allclose(q_high.to_numpy(), q_low.to_numpy())

    # Causality via MS12 lookahead harness
    assert_future_append_invariant(cyq_cost_deviation, high_turn, [5, 15, 24])
    assert_future_append_invariant(cyq_quantile_spread_90_10, high_turn, [5, 15, 24])


# =============================================================================
# MS19: wyckoff_effort_result 5m Return Pearson Kurtosis Multiplier
# =============================================================================


def test_ms19_wyckoff_effort_result_kurtosis_multiplier_and_nan_contract():
    from common.structural_factors import (
        enrich_universe_with_structural_features,
        wyckoff_effort_result,
    )

    rng = np.random.default_rng(19)
    dates = pd.bdate_range("2025-03-03", periods=10)
    close = np.linspace(100.0, 110.0, 10)
    daily = pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(10, 10000.0),
        },
        index=dates,
    )

    # 1. Default kurtosis_multiplier=False is bit-identical to baseline
    base_er = wyckoff_effort_result(daily)
    off_er = wyckoff_effort_result(daily, kurtosis_multiplier=False, intraday_5m_bars=None)
    pd.testing.assert_series_equal(base_er, off_er)

    # 2. kurtosis_multiplier=True with None -> all NaN (never 1.0 fallback!)
    none_er = wyckoff_effort_result(daily, kurtosis_multiplier=True, intraday_5m_bars=None)
    assert none_er.isna().all()

    # 3. Complete 48-bar day vs incomplete 30-bar day
    frames_5m = []
    for i, d in enumerate(dates):
        morn = pd.date_range(f"{d.date()} 09:35:00", f"{d.date()} 11:30:00", freq="5min")
        if i == 8:
            # Incomplete session: 30 bars (< 45) -> must emit NaN
            aft = pd.date_range(f"{d.date()} 13:05:00", f"{d.date()} 13:30:00", freq="5min")
        else:
            aft = pd.date_range(f"{d.date()} 13:05:00", f"{d.date()} 15:00:00", freq="5min")
        idx = morn.append(aft)
        c5 = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.002, size=len(idx))))
        frames_5m.append(pd.DataFrame({"Close": c5}, index=idx))
    df_5m = pd.concat(frames_5m)

    kurt_er = wyckoff_effort_result(daily, kurtosis_multiplier=True, intraday_5m_bars=df_5m)
    assert np.isfinite(kurt_er.iloc[9])
    assert kurt_er.iloc[9] != pytest.approx(base_er.iloc[9], rel=1e-3)
    assert np.isnan(kurt_er.iloc[8])  # incomplete 30-bar session -> NaN

    # 4. enrich_universe_with_structural_features stays on 12 baseline columns
    enriched = enrich_universe_with_structural_features({"A": daily})["A"]
    pd.testing.assert_series_equal(
        enriched["wyckoff_effort_result"],
        wyckoff_effort_result(daily, lag=5, volume_lookback=20),
        check_names=False,
    )


def test_w11_high1b_partial_nan_turn_fallback_and_use_real_turn_isolation():
    """W1.1 HIGH-1b: partial-NaN Turn falls back bar-by-bar to 60d volume proxy,
    turn_stats_out reports exact counts, and enrich_universe_with_structural_features
    defaults to use_real_turn=False (zero drift when Turn is present)."""
    from common.structural_factors import (
        cyq_chip_distribution,
        cyq_cost_deviation,
        enrich_universe_with_structural_features,
    )

    dates = pd.bdate_range("2025-01-02", periods=30)
    close = np.linspace(100.0, 140.0, 30)
    # Varying volume so 60d volume proxy differs materially from flat 0.02
    vol = np.linspace(1000.0, 8000.0, 30)
    base = pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": vol,
        },
        index=dates,
    )

    # 1. Partial-NaN Turn: only the very last bar has real Turn; prior 29 bars are NaN.
    # Because bar 0..28 are NaN, their chip distribution MUST match the pure-proxy run
    # bar-for-bar up through bar 28 (NOT degrade to flat 0.02!).
    partial = base.copy()
    partial["Turn"] = np.nan
    partial.iloc[-1, partial.columns.get_loc("Turn")] = 25.0

    stats_partial = {}
    cd_partial = cyq_cost_deviation(partial, turn_stats_out=stats_partial)
    stats_proxy = {}
    cd_proxy = cyq_cost_deviation(base, turn_stats_out=stats_proxy)

    assert stats_partial == {
        "used_real_turn": True,
        "real_turn_bars": 1,
        "proxy_turn_bars": 29,
        "turn_mode": "relative",
    }
    assert stats_proxy == {
        "used_real_turn": False,
        "real_turn_bars": 0,
        "proxy_turn_bars": 30,
        "turn_mode": "relative",
    }
    # Bars 0..28 must be 100% bit-identical to pure proxy; bar 29 diverges due to real Turn=25%
    np.testing.assert_array_equal(cd_partial.iloc[:-1].to_numpy(), cd_proxy.iloc[:-1].to_numpy())
    assert cd_partial.iloc[-1] != cd_proxy.iloc[-1]

    # 2. use_real_turn=False on a frame WITH Turn is 100% bit-identical to no-Turn frame
    full_turn = base.assign(Turn=15.0)
    wr_off, cc_off = cyq_chip_distribution(full_turn, use_real_turn=False)
    wr_no, cc_no = cyq_chip_distribution(base)
    pd.testing.assert_series_equal(wr_off, wr_no)
    pd.testing.assert_series_equal(cc_off, cc_no)

    # 3. enrich_universe_with_structural_features defaults to use_real_turn=False
    enr_with = enrich_universe_with_structural_features({"A": full_turn})["A"]
    enr_without = enrich_universe_with_structural_features({"A": base})["A"]
    pd.testing.assert_series_equal(enr_with["cyq_winner_ratio"], enr_without["cyq_winner_ratio"])
    pd.testing.assert_series_equal(
        enr_with["cyq_cost_concentration"], enr_without["cyq_cost_concentration"]
    )


def test_ms27_relative_turn_mode_scale_invariance_and_raw_divergence():
    """MS27 (Arm E): turn_mode='relative' is scale-invariant across mega-cap (0.5% base Turn)
    vs mid-cap (10.0% base Turn) sharing the same relative turnover spike profile, whereas
    turn_mode='raw' diverges; invalid turn_mode raises ValueError."""
    from common.structural_factors import (
        cyq_chip_distribution,
        cyq_cost_deviation,
        cyq_quantile_spread_90_10,
    )

    dates = pd.bdate_range("2025-01-02", periods=80)
    rng = np.random.default_rng(27)
    close = 50.0 * np.exp(np.cumsum(rng.normal(0.001, 0.015, size=80)))
    rel_spike = rng.uniform(0.5, 3.0, size=80)
    base = pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.015,
            "Low": close * 0.985,
            "Close": close,
            "Volume": np.full(80, 10000.0),
        },
        index=dates,
    )
    mega_cap = base.assign(Turn=0.5 * rel_spike)   # ~0.5% base daily turnover
    mid_cap = base.assign(Turn=10.0 * rel_spike)   # ~10.0% base daily turnover (20x scale)

    # Under turn_mode="relative" (default), both stocks produce identical CYQ series
    stats_mega: dict = {}
    stats_mid: dict = {}
    cd_mega_rel = cyq_cost_deviation(mega_cap, turn_mode="relative", turn_stats_out=stats_mega)
    cd_mid_rel = cyq_cost_deviation(mid_cap, turn_mode="relative", turn_stats_out=stats_mid)
    qs_mega_rel = cyq_quantile_spread_90_10(mega_cap, turn_mode="relative")
    qs_mid_rel = cyq_quantile_spread_90_10(mid_cap, turn_mode="relative")

    np.testing.assert_allclose(cd_mega_rel.to_numpy(), cd_mid_rel.to_numpy(), rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(qs_mega_rel.to_numpy(), qs_mid_rel.to_numpy(), rtol=1e-6, atol=1e-6)
    assert stats_mega["turn_mode"] == "relative"
    assert stats_mid["turn_mode"] == "relative"
    assert stats_mega["real_turn_bars"] == 80

    # Under turn_mode="raw", the 20x turnover level difference causes material divergence
    cd_mega_raw = cyq_cost_deviation(mega_cap, turn_mode="raw")
    cd_mid_raw = cyq_cost_deviation(mid_cap, turn_mode="raw")
    assert float((cd_mega_raw - cd_mid_raw).abs().max()) > 1e-2

    # Invalid turn_mode raises ValueError (non-empty and empty frames)
    with pytest.raises(ValueError, match="turn_mode"):
        cyq_cost_deviation(mega_cap, turn_mode="bogus")
    with pytest.raises(ValueError, match="turn_mode"):
        cyq_chip_distribution(mega_cap.iloc[:0], turn_mode="bogus")


def test_cyq_5m_pregrouped_kernel_matches_legacy_reference_and_edge_cases():
    """Zero-drift equivalence of pre-grouped 5m CYQ kernel vs literal legacy .iterrows()
    reference across edge cases, strict causality, and offline speed check."""
    import time
    from common.structural_factors import (
        cyq_chip_distribution,
        cyq_cost_deviation,
        cyq_quantile_spread_90_10,
    )

    def _legacy_cyq_reference(bars, intraday_5m_bars, n_bins=50, turn_mode="relative"):
        n_bars = len(bars)
        vol = bars["Volume"].fillna(1.0).astype(float)
        sma_60 = vol.rolling(60, min_periods=1).mean()
        proxy_tau = (0.02 * (vol / (sma_60 + 1e-8))).clip(lower=0.002, upper=0.50)
        tau_series = proxy_tau
        if "Turn" in bars.columns and not bars["Turn"].isna().all():
            col_s = pd.to_numeric(bars["Turn"], errors="coerce").astype(float)
            valid_mask = col_s.notna() & np.isfinite(col_s)
            if bool(valid_mask.any()):
                if turn_mode == "relative":
                    nt = col_s.where(valid_mask).clip(lower=0.0)
                    scaled = (0.02 * (nt / (nt.rolling(60, min_periods=1).mean() + 1e-8))).clip(lower=0.002, upper=0.50)
                else:
                    med = float(col_s[valid_mask].median())
                    scaled = (col_s / 100.0 if med > 1.0 else col_s).clip(lower=0.001, upper=0.99)
                tau_series = scaled.where(valid_mask, other=proxy_tau)

        lows = bars["Low"].to_numpy(dtype=float)
        highs = bars["High"].to_numpy(dtype=float)
        closes = bars["Close"].to_numpy(dtype=float)
        l_0, h_0 = lows[0], highs[0]
        if h_0 <= l_0:
            h_0 = l_0 * 1.01 + 0.01
            l_0 = l_0 * 0.99 - 0.01
        p_min, p_max = l_0 * 0.95, h_0 * 1.05
        if p_max <= p_min:
            p_max = p_min + 1.0
        bin_edges = np.linspace(p_min, p_max, n_bins + 1)
        chips = np.zeros(n_bins, dtype=float)
        wr = np.zeros(n_bars, dtype=float)
        cc = np.zeros(n_bars, dtype=float)
        cd = np.full(n_bars, np.nan, dtype=float)
        qs = np.full(n_bars, np.nan, dtype=float)

        for t in range(n_bars):
            l_t, h_t, c_t = lows[t], highs[t], closes[t]
            if h_t <= l_t:
                h_t = l_t * 1.001 + 0.001
                l_t = l_t * 0.999 - 0.001
            cand_min, cand_max = l_t * 0.95, h_t * 1.05
            if cand_min < p_min or cand_max > p_max:
                new_p_min, new_p_max = min(p_min, cand_min), max(p_max, cand_max)
                new_edges = np.linspace(new_p_min, new_p_max, n_bins + 1)
                old_cdf = np.concatenate([[0.0], np.cumsum(chips)])
                chips = np.maximum(0.0, np.diff(np.interp(new_edges, bin_edges, old_cdf, left=0.0, right=1.0)))
                sc = float(np.sum(chips))
                chips = chips / sc if sc > 1e-12 else np.full(n_bins, 1.0 / n_bins)
                p_min, p_max, bin_edges = new_p_min, new_p_max, new_edges

            bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.0
            k_t = np.zeros(n_bins, dtype=float)
            used_5m = False
            if intraday_5m_bars is not None and len(intraday_5m_bars) > 0:
                day_5m = None
                if isinstance(intraday_5m_bars.index, pd.DatetimeIndex) and hasattr(bars.index[t], "date"):
                    day_5m = intraday_5m_bars[intraday_5m_bars.index.date == bars.index[t].date()]
                elif hasattr(bars.index[t], "strftime"):
                    cur_str = bars.index[t].strftime("%Y-%m-%d")
                    for c in ["date", "Date", "datetime", "Datetime"]:
                        if c in intraday_5m_bars.columns:
                            day_5m = intraday_5m_bars[intraday_5m_bars[c].astype(str).str.startswith(cur_str)]
                            break
                if day_5m is not None and not day_5m.empty:
                    for _, r in day_5m.iterrows():
                        ml, mh = float(r["Low"]), float(r["High"])
                        mv = float(r["Volume"]) if ("Volume" in r and not np.isnan(r["Volume"])) else 1.0
                        bl = max(0, min(n_bins - 1, int(np.searchsorted(bin_edges, ml, side="right") - 1)))
                        bh = max(0, min(n_bins - 1, int(np.searchsorted(bin_edges, mh, side="left"))))
                        if bh < bl:
                            bh = bl
                        k_t[bl : bh + 1] += mv / (bh - bl + 1)
                    sk = float(np.sum(k_t))
                    if sk > 1e-12:
                        k_t /= sk
                        used_5m = True
            if not used_5m:
                i_start = max(0, min(n_bins - 1, int(np.searchsorted(bin_edges, l_t, side="right") - 1)))
                i_end = max(0, min(n_bins - 1, int(np.searchsorted(bin_edges, h_t, side="left"))))
                if i_end < i_start:
                    i_end = i_start
                if i_start == i_end:
                    k_t[i_start : i_end + 1] = 1.0
                else:
                    p_peak = float(np.clip((h_t + l_t + c_t) / 3.0, l_t, h_t))
                    w = np.zeros(i_end - i_start + 1, dtype=float)
                    for idx_w, j in enumerate(range(i_start, i_end + 1)):
                        m = bin_mids[j]
                        if m <= p_peak:
                            w[idx_w] = max(0.0, 1.0 - (p_peak - m) / (p_peak - l_t + 1e-8)) if p_peak > l_t else 1.0
                        else:
                            w[idx_w] = max(0.0, 1.0 - (m - p_peak) / (h_t - p_peak + 1e-8)) if h_t > p_peak else 1.0
                    sw = float(np.sum(w))
                    k_t[i_start : i_end + 1] = (w / sw) if sw > 1e-12 else (1.0 / (i_end - i_start + 1))

            if t == 0:
                chips = k_t.copy()
            else:
                tau_t = float(tau_series.iloc[t])
                chips = chips * (1.0 - tau_t) + tau_t * k_t
                chips /= float(np.sum(chips))

            wr[t] = float(np.clip(np.sum(chips[bin_mids <= c_t]), 0.0, 1.0))
            cdf = np.concatenate([[0.0], np.cumsum(chips)])
            cdf[-1] = 1.0
            p5, p95 = float(np.interp(0.05, cdf, bin_edges)), float(np.interp(0.95, cdf, bin_edges))
            cc[t] = float(np.clip(1.0 - (p95 - p5) / (p95 + p5 + 1e-8), 0.0, 1.0))
            p_cost = float(np.sum(chips * bin_mids))
            if np.isfinite(p_cost) and p_cost > 1e-12:
                cd[t] = (c_t - p_cost) / p_cost
            p10, p50, p90 = (
                float(np.interp(0.10, cdf, bin_edges)),
                float(np.interp(0.50, cdf, bin_edges)),
                float(np.interp(0.90, cdf, bin_edges)),
            )
            if np.isfinite(p50) and p50 > 1e-12:
                qs[t] = (p90 - p10) / p50
        return wr, cc, cd, qs

    rng = np.random.default_rng(123)
    dates = pd.bdate_range("2025-02-03", periods=25)
    close = 20.0 * np.exp(np.cumsum(rng.normal(0.002, 0.02, size=len(dates))))
    daily = pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.025,
            "Low": close * 0.975,
            "Close": close,
            "Volume": rng.uniform(5e5, 2e6, size=len(dates)),
            "Turn": rng.uniform(0.5, 4.0, size=len(dates)),
        },
        index=dates,
    )

    # Build 5m bars with partial missing days (skip days 3, 11, 18 -> triangular fallback)
    frames = []
    for i, d in enumerate(dates):
        if i in (3, 11, 18):
            continue
        idx5 = pd.date_range(f"{d.date()} 09:35:00", f"{d.date()} 11:30:00", freq="5min").append(
            pd.date_range(f"{d.date()} 13:05:00", f"{d.date()} 15:00:00", freq="5min")
        )
        mid = daily["Close"].iloc[i] * (1.0 + rng.normal(0.0, 0.008, size=len(idx5)))
        frames.append(
            pd.DataFrame(
                {
                    "Low": mid * 0.996,
                    "High": mid * 1.004,
                    "Close": mid,
                    "Volume": rng.uniform(1e4, 5e4, size=len(idx5)),
                },
                index=idx5,
            )
        )
    df_5m = pd.concat(frames)
    df_5m_nan_vol = df_5m.copy()
    df_5m_nan_vol.iloc[::7, df_5m_nan_vol.columns.get_loc("Volume")] = np.nan
    df_5m_no_vol = df_5m.drop(columns=["Volume"])
    df_5m_tz = df_5m.copy()
    df_5m_tz.index = df_5m_tz.index.tz_localize("Asia/Shanghai")
    df_5m_unsorted = df_5m.sample(frac=1.0, random_state=7)
    df_5m_col = df_5m.reset_index().rename(columns={"index": "datetime"})
    df_5m_col["datetime"] = df_5m_col["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")

    cases = [
        ("relative", df_5m),
        ("raw", df_5m),
        ("relative", df_5m_nan_vol),
        ("relative", df_5m_no_vol),
        ("relative", df_5m_tz),
        ("relative", df_5m_unsorted),
        ("relative", df_5m_col),
    ]
    for mode, f5m in cases:
        wr_ref, cc_ref, cd_ref, qs_ref = _legacy_cyq_reference(daily, f5m, n_bins=50, turn_mode=mode)
        wr, cc = cyq_chip_distribution(daily, n_bins=50, intraday_5m_bars=f5m, turn_mode=mode)
        cd = cyq_cost_deviation(daily, n_bins=50, intraday_5m_bars=f5m, turn_mode=mode)
        qs = cyq_quantile_spread_90_10(daily, n_bins=50, intraday_5m_bars=f5m, turn_mode=mode)
        np.testing.assert_allclose(wr.to_numpy(), wr_ref, rtol=0.0, atol=1e-12, equal_nan=True)
        np.testing.assert_allclose(cc.to_numpy(), cc_ref, rtol=0.0, atol=1e-12, equal_nan=True)
        np.testing.assert_allclose(cd.to_numpy(), cd_ref, rtol=0.0, atol=1e-12, equal_nan=True)
        np.testing.assert_allclose(qs.to_numpy(), qs_ref, rtol=0.0, atol=1e-12, equal_nan=True)

    # Strict causality with 5m bars attached
    wr_full, cc_full = cyq_chip_distribution(daily, n_bins=50, intraday_5m_bars=df_5m)
    cd_full = cyq_cost_deviation(daily, n_bins=50, intraday_5m_bars=df_5m)
    cut = 15
    cut_date = dates[cut - 1]
    df_5m_pre = df_5m.loc[df_5m.index <= cut_date + pd.Timedelta(hours=23, minutes=59)]
    wr_pre, cc_pre = cyq_chip_distribution(daily.iloc[:cut], n_bins=50, intraday_5m_bars=df_5m_pre)
    cd_pre = cyq_cost_deviation(daily.iloc[:cut], n_bins=50, intraday_5m_bars=df_5m_pre)
    np.testing.assert_allclose(wr_pre.to_numpy(), wr_full.iloc[:cut].to_numpy(), rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(cc_pre.to_numpy(), cc_full.iloc[:cut].to_numpy(), rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(cd_pre.to_numpy(), cd_full.iloc[:cut].to_numpy(), rtol=0.0, atol=1e-12)

    # Offline performance benchmark: 250 daily bars x 48 5m bars/day = 12,000 rows (< 2.0s)
    b_dates = pd.bdate_range("2024-01-02", periods=250)
    b_close = 30.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, size=250)))
    b_daily = pd.DataFrame(
        {
            "Open": b_close * 0.995,
            "High": b_close * 1.02,
            "Low": b_close * 0.98,
            "Close": b_close,
            "Volume": rng.uniform(5e5, 2e6, size=250),
            "Turn": rng.uniform(0.5, 4.0, size=250),
        },
        index=b_dates,
    )
    b_5m_idx = pd.DatetimeIndex(
        np.concatenate(
            [
                pd.date_range(f"{d.date()} 09:35:00", f"{d.date()} 11:30:00", freq="5min")
                .append(pd.date_range(f"{d.date()} 13:05:00", f"{d.date()} 15:00:00", freq="5min"))
                .to_numpy()
                for d in b_dates
            ]
        )
    )
    b_mid = np.repeat(b_close, 48) * (1.0 + rng.normal(0.0, 0.005, size=len(b_5m_idx)))
    b_5m = pd.DataFrame(
        {"Low": b_mid * 0.997, "High": b_mid * 1.003, "Close": b_mid, "Volume": rng.uniform(1e4, 5e4, size=len(b_5m_idx))},
        index=b_5m_idx,
    )
    t0 = time.perf_counter()
    _ = cyq_cost_deviation(b_daily, n_bins=50, intraday_5m_bars=b_5m, turn_mode="relative")
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"Pre-grouped 5m CYQ took {elapsed:.3f}s (expected < 2.0s)"

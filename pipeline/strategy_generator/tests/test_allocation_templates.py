import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes as make_df

from common.allocation_templates import (
    ALLOCATION_TEMPLATES,
    BreadthGatedMomentumAllocation,
    CrossSectionalMomentumAllocation,
    DualMomentumAllocation,
    EqualWeightAllocation,
    HierarchicalRiskParityAllocation,
    InverseVolatilityAllocation,
    MaxDiversificationAllocation,
    MeanReversionAllocation,
    MinimumVarianceAllocation,
    PatternBasedAllocationTemplate,
)


def test_equal_weight_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    universe = {
        "A": pd.DataFrame({"Close": np.ones(100)}, index=idx),
        "B": pd.DataFrame({"Close": np.ones(100)}, index=idx),
        "C": pd.DataFrame({"Close": np.ones(100)}, index=idx),
    }

    template = EqualWeightAllocation()
    weights = template.generate_weights(universe, {"rebalance_freq_days": 10})

    assert list(weights.columns) == ["A", "B", "C"]
    assert len(weights) == 100

    # Sparse: only the 10 actual rebalance-date rows (every 10 of 100 days)
    # carry a value, everything else is NaN -- the backtester relies on this
    # to tell "rebalanced to the same 1/N weight again" apart from "no
    # rebalance happened" (see allocation_backtester.py).
    rebalance_rows = weights.dropna(how="all")
    assert len(rebalance_rows) == 10

    # Check that weights sum to 1.0 (or very close to it) on every rebalance date
    np.testing.assert_allclose(rebalance_rows.sum(axis=1), 1.0)

    # Check individual weights are 1/3
    np.testing.assert_allclose(rebalance_rows["A"], 1.0 / 3.0)


def test_inverse_volatility_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)

    # A is flat (low vol), B is volatile
    rng = np.random.default_rng(42)
    closes_a = 100 + np.cumsum(rng.normal(0, 0.1, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 5.0, 100))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = InverseVolatilityAllocation()
    weights = template.generate_weights(universe, {"vol_lookback": 20, "rebalance_freq_days": 10})

    # After the 20-day warmup, A should have a much higher weight than B
    assert weights.iloc[30]["A"] > weights.iloc[30]["B"]
    np.testing.assert_allclose(weights.iloc[30].sum(), 1.0)


def test_cross_sectional_momentum_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)

    # A goes up, B goes down, C stays flat
    closes_a = np.linspace(100, 200, 100)
    closes_b = np.linspace(100, 50, 100)
    closes_c = np.full(100, 100)

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": make_df(closes_c, start="2020-01-01"),
    }

    template = CrossSectionalMomentumAllocation()
    # Top 33% of 3 assets = top 1 asset
    weights = template.generate_weights(universe, {"mom_lookback": 20, "top_n_fraction": 0.33, "rebalance_freq_days": 10})

    # After warmup, asset A (the only one going up) should get 100% of the weight
    np.testing.assert_allclose(weights.iloc[30]["A"], 1.0)
    np.testing.assert_allclose(weights.iloc[30]["B"], 0.0)
    np.testing.assert_allclose(weights.iloc[30]["C"], 0.0)


def test_hierarchical_risk_parity_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(10)
    closes_a = 100 + np.cumsum(rng.normal(0, 0.1, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 2.0, 100))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = HierarchicalRiskParityAllocation()
    weights = template.generate_weights(universe, {"cov_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    # Weights should sum to 1.0
    np.testing.assert_allclose(rebalance_rows.sum(axis=1), 1.0)
    # A (lower vol) gets higher HRP weight than B (higher vol)
    assert rebalance_rows.iloc[-1]["A"] > rebalance_rows.iloc[-1]["B"]


def test_hierarchical_risk_parity_excludes_a_symbol_with_no_data_in_window():
    # Regression test: a symbol that hasn't started trading yet (all-NaN
    # Close for the whole lookback window -- e.g. a newer ETF mixed into an
    # older basket) used to have its covariance zero-filled, which
    # inverse-variance weighting misreads as "risk-free" and hands almost
    # the entire portfolio to. It must instead be excluded from that
    # rebalance date's weights (left at 0 once the backtester fills it in),
    # not dominate them. All synthetic data, no network/market data involved.
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(7)
    closes_a = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 1.0, 100))

    # C has no price history at all for the first 90 bars (NaN Close) --
    # every 20-day lookback window before bar 90 is entirely NaN for C.
    closes_c = np.full(100, np.nan)
    closes_c[90:] = 100 + np.cumsum(rng.normal(0, 1.0, 10))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": pd.DataFrame({"Close": closes_c}, index=idx),
    }

    template = HierarchicalRiskParityAllocation()
    weights = template.generate_weights(universe, {"cov_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty

    # Every rebalance date before C has any data must NOT allocate to C --
    # it should be excluded (NaN in this sparse frame, later filled to 0.0
    # by the backtester), not dominate the portfolio.
    early_rows = rebalance_rows[rebalance_rows.index < idx[90]]
    assert not early_rows.empty
    assert early_rows["C"].isna().all()
    # A and B alone still sum to 1.0 -- properly renormalized among just the
    # symbols that actually had data, not diluted by a phantom C weight.
    np.testing.assert_allclose(early_rows["A"] + early_rows["B"], 1.0)


def test_dual_momentum_allocation_steps_to_cash_when_trend_negative():
    idx = pd.bdate_range("2020-01-01", periods=100)

    # A and B both go down
    closes_a = np.linspace(100, 50, 100)
    closes_b = np.linspace(100, 30, 100)

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = DualMomentumAllocation()
    weights = template.generate_weights(universe, {"mom_lookback": 20, "top_n_fraction": 0.5, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    # After warmup, both assets have negative trailing returns -> absolute momentum filter sets weights to 0 (cash)
    last_row = rebalance_rows.iloc[-1]
    np.testing.assert_allclose(last_row["A"], 0.0)
    np.testing.assert_allclose(last_row["B"], 0.0)


def test_max_diversification_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(20)

    closes_a = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 1.0, 100))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = MaxDiversificationAllocation()
    weights = template.generate_weights(universe, {"vol_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    np.testing.assert_allclose(rebalance_rows.sum(axis=1), 1.0)


def test_warmup_bars_reports_each_templates_indicator_lookback():
    # A caller slicing a sub-window (e.g. backtester/run_backtest.py's
    # run_walkforward) needs to know how much history to pull in ahead of
    # that window so the template's own indicator isn't cold at the window's
    # start -- this is the contract each template must expose.
    assert EqualWeightAllocation().warmup_bars({"rebalance_freq_days": 21}) == 0
    assert InverseVolatilityAllocation().warmup_bars({"vol_lookback": 60, "rebalance_freq_days": 21}) == 60
    assert CrossSectionalMomentumAllocation().warmup_bars(
        {"mom_lookback": 126, "top_n_fraction": 0.5, "rebalance_freq_days": 21}
    ) == 126
    assert HierarchicalRiskParityAllocation().warmup_bars({"cov_lookback": 126, "rebalance_freq_days": 21}) == 126
    assert DualMomentumAllocation().warmup_bars({"mom_lookback": 126, "top_n_fraction": 0.5, "rebalance_freq_days": 21}) == 126
    assert MaxDiversificationAllocation().warmup_bars({"vol_lookback": 126, "rebalance_freq_days": 21}) == 126
    assert MeanReversionAllocation().warmup_bars({"rsi_period": 5, "top_n_fraction": 0.5, "rebalance_freq_days": 21}) == 5
    assert MinimumVarianceAllocation().warmup_bars({"cov_lookback": 126, "rebalance_freq_days": 21}) == 126
    assert BreadthGatedMomentumAllocation().warmup_bars(
        {"mom_lookback": 126, "top_n_fraction": 0.5, "protection_factor": 1, "rebalance_freq_days": 21}
    ) == 126


def test_mean_reversion_allocation_picks_the_most_oversold_symbol():
    idx = pd.bdate_range("2020-01-01", periods=100)

    # A has just crashed hard right before the rebalance (oversold -> low RSI);
    # B has just spiked hard (overbought -> high RSI).
    closes_a = np.concatenate([np.full(90, 100.0), np.linspace(100, 80, 10)])
    closes_b = np.concatenate([np.full(90, 100.0), np.linspace(100, 120, 10)])

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = MeanReversionAllocation()
    weights = template.generate_weights(universe, {"rsi_period": 2, "top_n_fraction": 0.5, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    last_row = rebalance_rows.iloc[-1]
    # top_n_fraction=0.5 of 2 symbols -> top_n=1 -> only the most oversold (A) gets weight
    np.testing.assert_allclose(last_row["A"], 1.0)
    np.testing.assert_allclose(last_row["B"], 0.0)


def test_minimum_variance_allocation():
    rng = np.random.default_rng(10)
    closes_a = 100 + np.cumsum(rng.normal(0, 0.1, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 2.0, 100))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = MinimumVarianceAllocation()
    weights = template.generate_weights(universe, {"cov_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    np.testing.assert_allclose(rebalance_rows.sum(axis=1), 1.0, atol=1e-6)
    # A (much lower vol) should get a materially higher min-variance weight than B.
    assert rebalance_rows.iloc[-1]["A"] > rebalance_rows.iloc[-1]["B"]


def test_minimum_variance_allocation_excludes_a_symbol_with_no_data_in_window():
    # Same regression pattern as HRP's own equivalent test: a symbol with no
    # price history over the lookback window must be excluded, not zero-filled
    # into the covariance matrix (which would look "risk-free" to the optimizer).
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(7)
    closes_a = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 1.0, 100))

    closes_c = np.full(100, np.nan)
    closes_c[90:] = 100 + np.cumsum(rng.normal(0, 1.0, 10))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": pd.DataFrame({"Close": closes_c}, index=idx),
    }

    template = MinimumVarianceAllocation()
    weights = template.generate_weights(universe, {"cov_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    early_rows = rebalance_rows[rebalance_rows.index < idx[90]]
    assert not early_rows.empty
    assert early_rows["C"].isna().all()
    np.testing.assert_allclose(early_rows["A"] + early_rows["B"], 1.0, atol=1e-6)


def test_breadth_gated_momentum_invested_fraction_shrinks_with_breadth():
    idx = pd.bdate_range("2020-01-01", periods=300)

    # 4 symbols all deterministically, unambiguously trending up (no noise,
    # matching test_cross_sectional_momentum_allocation's own style above) ->
    # full breadth -> fully invested.
    up_universe = {
        sym: make_df(np.linspace(100, 100 + 10 * (i + 1), 300), start="2020-01-01")
        for i, sym in enumerate(["A", "B", "C", "D"])
    }
    template = BreadthGatedMomentumAllocation()
    params = {"mom_lookback": 63, "top_n_fraction": 0.5, "protection_factor": 1, "rebalance_freq_days": 21}
    up_weights = template.generate_weights(up_universe, params)
    up_rebal = up_weights.dropna(how="all")
    assert not up_rebal.empty
    np.testing.assert_allclose(up_rebal.iloc[-1].sum(), 1.0, atol=1e-6)

    # 4 symbols all deterministically trending down -> zero breadth -> fully de-risked (all cash).
    down_universe = {
        sym: make_df(np.linspace(100, 100 - 10 * (i + 1), 300), start="2020-01-01")
        for i, sym in enumerate(["A", "B", "C", "D"])
    }
    down_weights = template.generate_weights(down_universe, params)
    down_rebal = down_weights.dropna(how="all")
    assert not down_rebal.empty
    np.testing.assert_allclose(down_rebal.iloc[-1].sum(), 0.0, atol=1e-6)


def test_breadth_gated_momentum_small_universe_no_divide_by_zero():
    # N=1: n1 = protection_factor * N / 4 <= N/2 always, so denom=N-n1 is
    # always > 0 regardless of how small the basket is -- no ZeroDivisionError.
    idx = pd.bdate_range("2020-01-01", periods=200)
    closes = 100 + np.cumsum(np.random.default_rng(1).normal(0.1, 1.0, 200))
    universe = {"A": make_df(closes, start="2020-01-01")}

    template = BreadthGatedMomentumAllocation()
    weights = template.generate_weights(
        universe, {"mom_lookback": 63, "top_n_fraction": 1.0, "protection_factor": 2, "rebalance_freq_days": 21}
    )
    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty


def test_all_templates_declare_factor_tags():
    # Every template must declare at least one factor_tags entry so
    # strategy_generator's optional --factor-report hand-off can meaningfully
    # contextualize/tie-break its selection (see stratgen/generator.py).
    for cls in ALLOCATION_TEMPLATES:
        template = cls()
        assert isinstance(template.factor_tags, list)
        assert len(template.factor_tags) > 0, f"{template.name} has no factor_tags"


# --- PatternBasedAllocationTemplate ----------------------------------------

def test_pattern_based_template_is_not_in_the_static_list():
    # Deliberately excluded: it's not zero-arg constructible (its threshold
    # comes from mining a SPECIFIC basket first) -- see its own docstring.
    assert "PatternBasedAllocationTemplate" not in [cls.__name__ for cls in ALLOCATION_TEMPLATES]


def test_pattern_based_template_rejects_invalid_comparison_and_event_type():
    import pytest
    with pytest.raises(ValueError, match="comparison"):
        PatternBasedAllocationTemplate("rsi", 14, threshold=30.0, comparison="sideways", event_type="trough")
    with pytest.raises(ValueError, match="event_type"):
        PatternBasedAllocationTemplate("rsi", 14, threshold=30.0, comparison="below", event_type="plateau")


def test_pattern_based_template_factor_tags_reflect_the_mined_indicator():
    # factor_tags must reflect WHICH INDICATOR was mined, not just the
    # peak/trough direction -- this is what lets a mined template's
    # generator.py factor tie-break compare it against research_strategy's
    # trend/factor evidence for the right reference class (see
    # common.allocation_templates._FEATURE_FACTOR_TAGS and its docstring).
    expected = {
        "rsi": "mean_reversion",
        "stoch_k": "mean_reversion",
        "cci": "mean_reversion",
        "williams_r": "mean_reversion",
        "bb_pctb": "mean_reversion",
        "adx": "regime_trend_strength",
        "roc": "absolute_momentum_trend",
        "sma_rel": "absolute_momentum_trend",
        "macd_hist": "absolute_momentum_trend",
        "atr_pct": "volatility_targeting",
    }
    for feature_name, tag in expected.items():
        for event_type in ("peak", "trough"):
            template = PatternBasedAllocationTemplate(
                feature_name, 14, threshold=1.0, comparison="below", event_type=event_type
            )
            assert template.factor_tags == [tag], (
                f"{feature_name}/{event_type} expected tag {tag!r}, got {template.factor_tags!r}"
            )


def test_pattern_based_template_unknown_feature_falls_back_to_direction_heuristic():
    # A feature_name outside DEFAULT_FEATURE_MENU (shouldn't happen via
    # normal mining) degrades to the old peak/trough-only heuristic rather
    # than raising.
    peak = PatternBasedAllocationTemplate("made_up_feature", 14, threshold=1.0, comparison="below", event_type="peak")
    trough = PatternBasedAllocationTemplate("made_up_feature", 14, threshold=1.0, comparison="below", event_type="trough")
    assert peak.factor_tags == ["regime_trend_strength"]
    assert trough.factor_tags == ["mean_reversion"]


def test_pattern_based_template_trough_direction_invests_when_triggered():
    # A deliberately crashed-then-flat universe: RSI(14) stays low (oversold)
    # for a long stretch after the crash -- a trough-direction template
    # (comparison="below") should invest during that stretch.
    n = 200
    crash_then_flat = np.concatenate([np.linspace(100, 60, 40), np.full(n - 40, 60.0)])
    universe = {
        "A": make_df(crash_then_flat, start="2020-01-01"),
        "B": make_df(crash_then_flat, start="2020-01-01"),
    }
    template = PatternBasedAllocationTemplate("rsi", 14, threshold=40.0, comparison="below", event_type="trough")
    params = {"threshold_mult": 1.0, "hold_days": 21, "rebalance_freq_days": 5}
    weights = template.generate_weights(universe, params)
    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    # At least one rebalance during/after the crash should be invested.
    assert (rebalance_rows.sum(axis=1) > 0).any()


def test_pattern_based_template_peak_direction_derisks_when_triggered():
    # A deliberately sharply-rallying universe: ADX(14) rises during a strong
    # sustained trend -- a peak-direction template (comparison="above")
    # should de-risk (fall to cash) once ADX crosses the mined threshold.
    n = 200
    strong_rally = 100 + np.cumsum(np.full(n, 0.8))  # smooth, strong, sustained uptrend
    universe = {
        "A": make_df(strong_rally, start="2020-01-01"),
        "B": make_df(strong_rally, start="2020-01-01"),
    }
    template = PatternBasedAllocationTemplate("adx", 14, threshold=20.0, comparison="above", event_type="peak")
    params = {"threshold_mult": 1.0, "hold_days": 21, "rebalance_freq_days": 5}
    weights = template.generate_weights(universe, params)
    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    # Once ADX confirms the strong trend, later rebalances should be de-risked (0).
    assert (rebalance_rows.sum(axis=1) == 0).any()


def test_pattern_based_template_empty_universe_returns_empty_frame():
    template = PatternBasedAllocationTemplate("rsi", 14, threshold=30.0, comparison="below", event_type="trough")
    assert template.generate_weights({}, {"rebalance_freq_days": 21}).empty


def test_pattern_based_template_warmup_bars_includes_hold_days():
    template = PatternBasedAllocationTemplate("sma_rel", 50, threshold=0.0, comparison="below", event_type="trough")
    assert template.warmup_bars({"hold_days": 21}) == 71
    tuple_lookback_template = PatternBasedAllocationTemplate(
        "macd_hist", (12, 26, 9), threshold=0.0, comparison="above", event_type="peak"
    )
    assert tuple_lookback_template.warmup_bars({"hold_days": 10}) == 36

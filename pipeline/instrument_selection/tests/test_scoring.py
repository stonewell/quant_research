import numpy as np
import pandas as pd
import pytest

from selectorbot.config import SelectionConfig
from selectorbot.scoring import (
    DAILY_FACTOR_RAW_COLUMNS,
    DAILY_FACTOR_SCORE_COLUMNS,
    DAILY_FACTOR_WEIGHTS,
    DEFAULT_WEIGHTS,
    SECTOR_NEUTRAL_MIN_GROUP_SIZE,
    score_universe,
)
from common.sector_metadata import sector_neutral_demean


def make_metrics(**overrides):
    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 1e6, 5e7],
        "median_spread_pct": [0.01, 0.5, 0.1],
        "realized_vol_annualized_pct": [15.0, 40.0, 25.0],
        "pct_days_trending_adx": [70.0, 10.0, 40.0],
        "hurst": [0.75, 0.25, 0.50],
        "hurst_significant": [True, True, False],
        "hurst_p_value": [0.01, 0.01, 1.0],
        "avg_correlation_to_universe": [0.8, 0.1, 0.5],
        "history_years": [10.0, 1.0, 4.0],
    }, index=["TRENDY", "MEANREVY", "NEUTRAL"])
    for k, v in overrides.items():
        df[k] = v
    return df


def test_no_strategy_fit_columns_remain():
    scored = score_universe(make_metrics())
    for legacy_col in ["grid_strategy_fit", "trend_strategy_fit", "meanrev_strategy_fit",
                        "trend_fit_score", "meanrev_fit_score"]:
        assert legacy_col not in scored.columns


def test_predictability_score_is_direction_agnostic():
    scored = score_universe(make_metrics())
    # TRENDY (H=0.75, significant) and MEANREVY (H=0.25, significant) deviate
    # from 0.5 by the same amount -> equal predictability regardless of direction.
    assert scored.loc["TRENDY", "predictability_score"] == pytest.approx(scored.loc["MEANREVY", "predictability_score"])


def test_insignificant_hurst_gets_much_lower_predictability_score():
    scored = score_universe(make_metrics())
    # NEUTRAL's H=0.50 is at the midpoint anyway, but check the *significance*
    # gate independently using an off-midpoint, non-significant case.
    metrics = make_metrics()
    metrics.loc["NEUTRAL", "hurst"] = 0.75
    metrics.loc["NEUTRAL", "hurst_significant"] = False
    scored = score_universe(metrics)
    assert scored.loc["NEUTRAL", "predictability_score"] < scored.loc["TRENDY", "predictability_score"]


def test_diversification_score_favors_low_correlation_to_universe():
    scored = score_universe(make_metrics())
    # MEANREVY has the lowest avg_correlation_to_universe (0.1) -> highest diversification score.
    assert scored["diversification_score"].idxmax() == "MEANREVY"


def test_history_adequacy_score_caps_at_full_credit_years():
    scored = score_universe(make_metrics(), min_history_years_for_full_credit=4.0)
    assert scored.loc["TRENDY", "history_adequacy_score"] == pytest.approx(100.0)  # 10 years >= 4-year cap
    assert scored.loc["MEANREVY", "history_adequacy_score"] == pytest.approx(25.0)  # 1/4 of the way there
    assert scored.loc["NEUTRAL", "history_adequacy_score"] == pytest.approx(100.0)  # exactly at the cap


def test_overall_score_ignores_missing_etf_metadata_without_penalty():
    metrics_with_meta = make_metrics(expense_ratio=[0.001, 0.005, np.nan], total_assets=[1e11, 1e8, np.nan])
    scored = score_universe(metrics_with_meta)
    # NEUTRAL has no expense_ratio/AUM at all (e.g., a plain stock) -- its
    # overall score should still be a valid, non-NaN number.
    assert not np.isnan(scored.loc["NEUTRAL", "overall_selection_score"])
    assert np.isnan(scored.loc["NEUTRAL", "etf_expense_score"])
    # With the NaN correctly excluded from BOTH the ranking and the
    # denominator (pandas' default na_option="keep"), the two real values
    # rank 1-of-2 and 2-of-2 among the non-NaN population -- NOT 1-of-3 and
    # 2-of-3, which is what na_option="bottom" would wrongly produce by
    # counting NEUTRAL's NaN as a phantom third entry. etf_expense_score
    # favors a LOWER expense ratio, so TRENDY (0.001, the smaller of the
    # two) scores 50.0 and MEANREVY (0.005, the larger) scores 0.0.
    assert scored.loc["TRENDY", "etf_expense_score"] == pytest.approx(50.0)
    assert scored.loc["MEANREVY", "etf_expense_score"] == pytest.approx(0.0)


def test_pct_rank_based_scores_are_not_skewed_by_a_nan_peer():
    # 4 non-NaN expense ratios plus one NaN peer (NEUTRAL) in the universe:
    # the non-NaN symbols' etf_expense_score must be IDENTICAL to what
    # they'd get if the NaN-metadata symbol weren't in the universe at all
    # -- a NaN peer must never silently compress everyone else's percentile.
    extra = pd.DataFrame({
        "avg_dollar_volume": [2e8, 3e8],
        "median_spread_pct": [0.05, 0.08],
        "realized_vol_annualized_pct": [20.0, 30.0],
        "pct_days_trending_adx": [50.0, 60.0],
        "hurst": [0.6, 0.4],
        "hurst_significant": [True, True],
        "hurst_p_value": [0.01, 0.01],
        "avg_correlation_to_universe": [0.3, 0.6],
        "history_years": [5.0, 6.0],
    }, index=["EXTRA1", "EXTRA2"])
    metrics = pd.concat([make_metrics(), extra])
    metrics["expense_ratio"] = [0.001, 0.005, np.nan, 0.002, 0.01]
    metrics["total_assets"] = [1e11, 1e8, np.nan, 5e9, 2e7]

    scored_with_nan_peer = score_universe(metrics)
    scored_without_nan_peer = score_universe(metrics.drop(index="NEUTRAL"))

    for sym in ["TRENDY", "MEANREVY", "EXTRA1", "EXTRA2"]:
        assert scored_with_nan_peer.loc[sym, "etf_expense_score"] == pytest.approx(
            scored_without_nan_peer.loc[sym, "etf_expense_score"]
        )


def test_etf_expense_score_favors_lower_expense_ratio():
    metrics_with_meta = make_metrics(expense_ratio=[0.001, 0.02, 0.005], total_assets=[1e11, 1e8, 5e9])
    scored = score_universe(metrics_with_meta)
    assert scored["etf_expense_score"].idxmax() == "TRENDY"  # lowest expense ratio (0.001)


def test_overall_score_weighted_average_matches_manual_calculation_when_all_present():
    # No candlestick_edge or momentum_edge column here, so both those scores
    # are NaN and get renormalized out -- the expected value divides by the sum
    # of the weights actually present (the same graceful-degradation path a
    # plain stock hits).
    metrics_with_meta = make_metrics(expense_ratio=[0.001, 0.02, 0.005], total_assets=[1e11, 1e8, 5e9])
    scored = score_universe(metrics_with_meta)
    weights = {
        "liquidity_score": 0.30, "vol_adequacy_score": 0.20, "predictability_score": 0.07,
        "diversification_score": 0.15, "history_adequacy_score": 0.10,
        "etf_expense_score": 0.025, "etf_aum_score": 0.025,
    }
    row = scored.loc["TRENDY"]
    expected = sum(row[col] * w for col, w in weights.items()) / sum(weights.values())
    assert row["overall_selection_score"] == pytest.approx(expected)


def test_sector_neutral_flag_off_bit_identity():
    # Test 1: Flag-off bit-identity via independently pinned expectation (~1e-12 tol).
    # Verifies both default and explicit enable_sector_neutral=False match the exact
    # unneutralized weighted average calculation across all symbols.
    # Real weights verified from DEFAULT_WEIGHTS: candlestick_score=0.03, momentum_score=0.10.
    # Fixture provides non-NaN candlestick/momentum edges for >= 1 row so they are not renormalized out.
    metrics = make_metrics(
        expense_ratio=[0.001, 0.02, 0.005],
        total_assets=[1e11, 1e8, 5e9],
        candlestick_edge=[0.05, 0.02, np.nan],
        candlestick_significant=[True, False, False],
        momentum_edge=[0.10, -0.05, np.nan],
        momentum_significant=[True, True, False],
    )
    scored_default = score_universe(metrics)
    scored_explicit_off = score_universe(metrics, enable_sector_neutral=False)

    weights = {
        "liquidity_score": 0.30,
        "vol_adequacy_score": 0.20,
        "predictability_score": 0.07,
        "momentum_score": 0.10,
        "candlestick_score": 0.03,
        "diversification_score": 0.15,
        "history_adequacy_score": 0.10,
        "etf_expense_score": 0.025,
        "etf_aum_score": 0.025,
    }
    for sym in metrics.index:
        row = scored_default.loc[sym]
        present_w = {k: weights[k] for k in weights if pd.notna(row.get(k, np.nan))}
        expected_score = sum(row[k] * w for k, w in present_w.items()) / sum(present_w.values())

        # Bit-identity check with tol 1e-12:
        np.testing.assert_allclose(row["overall_selection_score"], expected_score, atol=1e-12, rtol=1e-12)
        np.testing.assert_allclose(
            scored_explicit_off.loc[sym, "overall_selection_score"],
            expected_score,
            atol=1e-12,
            rtol=1e-12,
        )


def test_sector_neutral_flag_on_equalizes_sector_means_to_global_mean():
    # Test 2: Flag-on demeaning with known sector bias.
    # S1, S2, S3 in 'technology' have high metric values,
    # S4, S5, S6 in 'utilities' have lower metric values.
    # Raw fixture is genuinely biased (sector means differ by > 30).
    #
    # HONEST INVARIANT PLACEMENT: "each qualifying sector's mean equals the global
    # mean" is a property of the DEMEAN (`sector_neutral_demean`), so it is asserted
    # below on the PRE-clip demeaned values. The downstream `np.clip(..., 0, 100)` in
    # `score_universe` is a boundary policy that CAN perturb this invariant at the
    # extremes (see test_sector_neutral_clip_perturbs_but_does_not_defeat_demean).
    # Asserting the invariant on the returned (already clipped) column would pass only
    # by accident of this fixture never clipping -- and would silently hide clip bias.
    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 8e8, 9e8, 1e6, 2e6, 1.5e6],
        "median_spread_pct": [0.01, 0.02, 0.015, 0.5, 0.4, 0.45],
        "realized_vol_annualized_pct": [20.0, 22.0, 21.0, 20.0, 22.0, 21.0],
        "pct_days_trending_adx": [60.0, 65.0, 62.0, 20.0, 25.0, 22.0],
        "hurst": [0.75, 0.70, 0.72, 0.50, 0.52, 0.51],
        "hurst_significant": [True, True, True, False, False, False],
        "hurst_p_value": [0.01, 0.01, 0.01, 0.8, 0.9, 0.85],
        "avg_correlation_to_universe": [0.3, 0.4, 0.35, 0.7, 0.75, 0.72],
        "history_years": [10.0, 8.0, 9.0, 3.0, 2.0, 2.5],
        "sector": ["technology", "technology", "technology", "utilities", "utilities", "utilities"],
    }, index=["S1", "S2", "S3", "S4", "S5", "S6"])

    # Unneutralized scores have strong sector bias:
    raw_scored = score_universe(df.copy(), enable_sector_neutral=False)
    raw_tech_mean = raw_scored.loc[["S1", "S2", "S3"], "overall_selection_score"].mean()
    raw_util_mean = raw_scored.loc[["S4", "S5", "S6"], "overall_selection_score"].mean()
    assert abs(raw_tech_mean - raw_util_mean) > 30.0
    assert abs(raw_tech_mean) > 10.0
    assert abs(raw_util_mean) > 10.0

    # Pre-clip demean, computed explicitly so the invariant is testable on its own terms:
    raw_scores = raw_scored["overall_selection_score"]
    pre_clip = sector_neutral_demean(
        raw_scores,
        sector=df["sector"],
        fallback="auto",
        min_group_size=SECTOR_NEUTRAL_MIN_GROUP_SIZE,
    )
    pre_clip_global_mean = pre_clip.mean()
    assert abs(pre_clip.loc[["S1", "S2", "S3"]].mean() - pre_clip_global_mean) < 1e-9
    assert abs(pre_clip.loc[["S4", "S5", "S6"]].mean() - pre_clip_global_mean) < 1e-9

    # This fixture stays inside [0, 100], so the clip is a pure no-op here and the
    # returned column must equal clip(pre_clip) exactly, elementwise:
    assert (pre_clip >= 0.0).all() and (pre_clip <= 100.0).all()
    neut_scored = score_universe(df.copy(), enable_sector_neutral=True)
    np.testing.assert_allclose(
        neut_scored["overall_selection_score"],
        np.clip(pre_clip, 0.0, 100.0),
        atol=1e-12,
    )


def test_sector_neutral_clip_perturbs_but_does_not_defeat_demean():
    # Known, ACCEPTED limitation, documented instead of hidden: the sector-mean
    # invariant belongs to the demean; `np.clip(..., 0, 100)` is a downstream
    # boundary policy that CAN perturb it at the extremes.
    # This fixture is deliberately built so the clip actually bites: T3 is worst on
    # nearly every metric (score ~3) inside an otherwise strong 'tech' sector, so the
    # demeaned value lands at ~ -4 and is clipped to 0.
    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 8e8, 1e5, 5e7, 4e7, 2e7, 1e7],
        "median_spread_pct": [0.01, 0.02, 0.9, 0.1, 0.12, 0.2, 0.3],
        "realized_vol_annualized_pct": [25.0, 22.0, 70.0, 20.0, 30.0, 40.0, 15.0],
        "pct_days_trending_adx": [60.0, 55.0, 20.0, 50.0, 45.0, 40.0, 35.0],
        "hurst": [0.90, 0.80, 0.50, 0.70, 0.65, 0.60, 0.55],
        "hurst_significant": [True, True, False, True, True, True, False],
        "hurst_p_value": [0.01, 0.01, 0.9, 0.02, 0.03, 0.04, 0.5],
        "avg_correlation_to_universe": [0.1, 0.2, 0.95, 0.4, 0.5, 0.6, 0.7],
        "history_years": [10.0, 9.0, 0.0, 5.0, 4.0, 3.0, 2.0],
        "sector": ["tech", "tech", "tech", "util", "util", "energy", "energy"],
    }, index=["T1", "T2", "T3", "U1", "U2", "E1", "E2"])

    raw_scored = score_universe(df.copy(), enable_sector_neutral=False)
    raw_scores = raw_scored["overall_selection_score"]
    pre_clip = sector_neutral_demean(
        raw_scores,
        sector=df["sector"],
        fallback="auto",
        min_group_size=SECTOR_NEUTRAL_MIN_GROUP_SIZE,
    )

    # 1. The clip genuinely bites in this fixture (this is the branch that would
    #    silently disappear if clipping were disabled or the fixture were weakened):
    assert (pre_clip < 0.0).any() or (pre_clip > 100.0).any()
    assert float(pre_clip.min()) < 0.0

    # 2. Despite the clip, the pre-clip values still satisfy the sector-mean
    #    invariant exactly -- neutralization is NOT defeated, the boundary merely
    #    truncates the extreme:
    pre_clip_global_mean = pre_clip.mean()
    assert abs(pre_clip.loc[["T1", "T2", "T3"]].mean() - pre_clip_global_mean) < 1e-9
    assert abs(pre_clip.loc[["U1", "U2"]].mean() - pre_clip_global_mean) < 1e-9
    assert abs(pre_clip.loc[["E1", "E2"]].mean() - pre_clip_global_mean) < 1e-9

    # 3. Production output is exactly the clipped pre-clip demean, i.e. the only
    #    difference from the invariant-holding series is the [0, 100] boundary:
    neut_scored = score_universe(df.copy(), enable_sector_neutral=True)
    returned = neut_scored["overall_selection_score"]
    np.testing.assert_allclose(returned, np.clip(pre_clip, 0.0, 100.0), atol=1e-12)
    assert (returned >= 0.0).all() and (returned <= 100.0).all()

    # 4. ...and the documented consequence: AFTER clipping, the sector mean no longer
    #    matches the global mean (the perturbation the invariant assertion above must
    #    not be pointed at):
    post_tech_mean = returned.loc[["T1", "T2", "T3"]].mean()
    post_global_mean = returned.mean()
    assert post_tech_mean > post_global_mean
    assert abs(post_tech_mean - post_global_mean) > 1e-6


def test_sector_neutral_single_group_identity():
    # 5b: Single-group identity -- universe where every symbol resolves to same sector
    # must come out essentially unchanged from raw score (group_mean == global_mean),
    # and emits single-group UserWarning.
    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 5e8, 1e7],
        "median_spread_pct": [0.01, 0.03, 0.1],
        "realized_vol_annualized_pct": [20.0, 25.0, 30.0],
        "pct_days_trending_adx": [60.0, 40.0, 20.0],
        "hurst": [0.7, 0.6, 0.5],
        "hurst_significant": [True, True, False],
        "hurst_p_value": [0.01, 0.02, 0.5],
        "avg_correlation_to_universe": [0.3, 0.5, 0.7],
        "history_years": [10.0, 5.0, 2.0],
        "sector": ["technology", "technology", "technology"],
    }, index=["A", "B", "C"])

    raw_scored = score_universe(df.copy(), enable_sector_neutral=False)
    with pytest.warns(UserWarning, match=r"all 3 symbols resolved to the single sector group 'technology'") as warn_records:
        neut_scored = score_universe(df.copy(), enable_sector_neutral=True)
    assert len(warn_records) == 1
    np.testing.assert_allclose(
        neut_scored["overall_selection_score"],
        raw_scored["overall_selection_score"],
        atol=1e-12,
    )


def test_sector_neutral_scale_preservation():
    # 5c: Scale preservation -- after neutralization on biased multi-sector fixture:
    # 1. max - min > 20
    # 2. all values within [0, 100]
    # 3. column preserves raw score ordering within each sector
    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 8e8, 9e8, 1e6, 2e6, 1.5e6],
        "median_spread_pct": [0.01, 0.02, 0.015, 0.5, 0.4, 0.45],
        "realized_vol_annualized_pct": [20.0, 22.0, 21.0, 20.0, 22.0, 21.0],
        "pct_days_trending_adx": [60.0, 65.0, 62.0, 20.0, 25.0, 22.0],
        "hurst": [0.75, 0.70, 0.72, 0.50, 0.52, 0.51],
        "hurst_significant": [True, True, True, False, False, False],
        "hurst_p_value": [0.01, 0.01, 0.01, 0.8, 0.9, 0.85],
        "avg_correlation_to_universe": [0.3, 0.4, 0.35, 0.7, 0.75, 0.72],
        "history_years": [10.0, 8.0, 9.0, 3.0, 2.0, 2.5],
        "sector": ["technology", "technology", "technology", "utilities", "utilities", "utilities"],
    }, index=["S1", "S2", "S3", "S4", "S5", "S6"])

    raw_scored = score_universe(df.copy(), enable_sector_neutral=False)
    neut_scored = score_universe(df.copy(), enable_sector_neutral=True)
    scores = neut_scored["overall_selection_score"]

    assert scores.max() - scores.min() > 20.0
    assert (scores >= 0.0).all() and (scores <= 100.0).all()

    # Preserves ranking within each sector
    tech_syms = ["S1", "S2", "S3"]
    util_syms = ["S4", "S5", "S6"]
    assert (
        np.sign(np.diff(scores.loc[tech_syms]))
        == np.sign(np.diff(raw_scored.loc[tech_syms, "overall_selection_score"]))
    ).all()
    assert (
        np.sign(np.diff(scores.loc[util_syms]))
        == np.sign(np.diff(raw_scored.loc[util_syms, "overall_selection_score"]))
    ).all()


def test_sector_neutral_noop_warning_on_all_singletons():
    # 5d: No-op warning -- fixture where no group qualifies (all singleton sectors)
    # warns UserWarning and scores equal raw scores exactly.
    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 1e7, 5e7],
        "median_spread_pct": [0.01, 0.1, 0.05],
        "realized_vol_annualized_pct": [15.0, 35.0, 25.0],
        "pct_days_trending_adx": [70.0, 20.0, 45.0],
        "hurst": [0.75, 0.30, 0.55],
        "hurst_significant": [True, True, False],
        "hurst_p_value": [0.01, 0.01, 0.5],
        "avg_correlation_to_universe": [0.8, 0.2, 0.5],
        "history_years": [10.0, 2.0, 5.0],
        "sector": ["sec_a", "sec_b", "sec_c"],
    }, index=["T1", "T2", "T3"])

    raw_scored = score_universe(df.copy(), enable_sector_neutral=False)
    with pytest.warns(UserWarning, match="no sector group had enough members") as warn_records:
        neut_scored = score_universe(df.copy(), enable_sector_neutral=True)

    assert len(warn_records) == 1
    np.testing.assert_allclose(
        neut_scored["overall_selection_score"],
        raw_scored["overall_selection_score"],
        atol=1e-12,
    )


def test_sector_neutral_preserves_row_count_and_index_order_when_absent_from_map():
    # Test 3: No symbol dropped; row count, index order and index name identical
    # even when symbols are absent from map or integer-indexed.
    metrics = make_metrics()
    metrics.index.name = "ticker_id"
    partial_map = {"UNKNOWN_SYM": "energy"}

    with pytest.warns(UserWarning, match=r"all 3 symbols resolved to the single sector group 'other'"):
        scored = score_universe(metrics, sector_map=partial_map, enable_sector_neutral=True)

    assert len(scored) == len(metrics)
    assert list(scored.index) == list(metrics.index)
    assert scored.index.name == metrics.index.name

    # Also verify with integer index
    metrics_int = make_metrics().reset_index(drop=True)
    metrics_int.index.name = "row_num"
    with pytest.warns(UserWarning, match=r"all 3 symbols resolved to the single sector group 'other'"):
        scored_int = score_universe(metrics_int, sector_map=partial_map, enable_sector_neutral=True)
    assert len(scored_int) == len(metrics_int)
    assert list(scored_int.index) == list(metrics_int.index)
    assert scored_int.index.name == metrics_int.index.name


def test_sector_neutral_degenerate_sectors_leave_raw_untouched_without_nan_or_exception():
    # Degenerate sector contract for demeaning:
    # Groups with < min_group_size (SOLO) or zero spread (EQ1, EQ2) leave raw scores untouched.
    # Qualifying multi-group members are demeaned so multi_mean == raw_global_mean.
    # No NaN anywhere, no exception.
    df = pd.DataFrame({
        "avg_dollar_volume": [1e8, 2e8, 3e8, 5e7, 1e7, 1e7],
        "median_spread_pct": [0.05, 0.04, 0.03, 0.10, 0.20, 0.20],
        "realized_vol_annualized_pct": [20.0, 22.0, 24.0, 25.0, 30.0, 30.0],
        "pct_days_trending_adx": [50.0, 55.0, 60.0, 40.0, 30.0, 30.0],
        "hurst": [0.6, 0.65, 0.7, 0.5, 0.5, 0.5],
        "hurst_significant": [True, True, True, False, False, False],
        "hurst_p_value": [0.01, 0.01, 0.01, 0.5, 0.5, 0.5],
        "avg_correlation_to_universe": [0.4, 0.5, 0.6, 0.3, 0.5, 0.5],
        "history_years": [5.0, 6.0, 7.0, 4.0, 2.0, 2.0],
        "sector": ["multi", "multi", "multi", "solo", "allequal", "allequal"],
    }, index=["M1", "M2", "M3", "SOLO", "EQ1", "EQ2"])

    raw_scored = score_universe(df.copy(), enable_sector_neutral=False)
    scored = score_universe(df.copy(), enable_sector_neutral=True)

    # Degenerate sectors (single member or zero variance) leave raw values untouched:
    np.testing.assert_allclose(
        scored.loc["SOLO", "overall_selection_score"],
        raw_scored.loc["SOLO", "overall_selection_score"],
        atol=1e-12,
    )
    np.testing.assert_allclose(
        scored.loc[["EQ1", "EQ2"], "overall_selection_score"],
        raw_scored.loc[["EQ1", "EQ2"], "overall_selection_score"],
        atol=1e-12,
    )

    # No NaN anywhere in the neutralized score column
    assert not scored["overall_selection_score"].isna().any()

    # Non-degenerate sector normalizes: its group mean matches raw global mean (atol 1e-9)
    raw_global_mean = raw_scored["overall_selection_score"].mean()
    multi_mean = scored.loc[["M1", "M2", "M3"], "overall_selection_score"].mean()
    assert abs(multi_mean - raw_global_mean) < 1e-9


def test_sector_neutral_config_defaults_and_offline():
    # Test 5: Confirm config defaults keep old behaviour (flag OFF, path None),
    # and no network/data providers are touched.
    cfg = SelectionConfig()
    assert cfg.enable_sector_neutral is False
    assert cfg.sector_map_path is None


def test_sector_neutral_noop_warning_on_single_group():
    # 5e: Single-group no-op warning -- universe whose symbols all resolve to one group
    # emits distinct single-group warning containing group name and symbol count,
    # and scores equal raw scores exactly.
    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 5e8, 1e7, 2e7],
        "median_spread_pct": [0.01, 0.03, 0.05, 0.08],
        "realized_vol_annualized_pct": [20.0, 25.0, 22.0, 30.0],
        "pct_days_trending_adx": [60.0, 40.0, 50.0, 20.0],
        "hurst": [0.7, 0.6, 0.65, 0.5],
        "hurst_significant": [True, True, True, False],
        "hurst_p_value": [0.01, 0.02, 0.01, 0.5],
        "avg_correlation_to_universe": [0.3, 0.5, 0.4, 0.7],
        "history_years": [10.0, 5.0, 6.0, 2.0],
    }, index=["SPY", "QQQ", "GLD", "IWM"])

    raw_scored = score_universe(df.copy(), enable_sector_neutral=False)
    with pytest.warns(UserWarning, match=r"all 4 symbols resolved to the single sector group 'other'") as warn_records:
        neut_scored = score_universe(df.copy(), enable_sector_neutral=True)

    assert len(warn_records) == 1
    msg = str(warn_records[0].message)
    assert "'other'" in msg
    assert "all 4 symbols" in msg
    assert "there is no between-sector structure to remove" in msg

    np.testing.assert_allclose(
        neut_scored["overall_selection_score"],
        raw_scored["overall_selection_score"],
        atol=1e-12,
    )


def test_sector_neutral_normal_multisector_emits_no_warning():
    # 5f: Normal multi-sector fixture with multiple qualifying groups emits NO warning.
    import warnings

    df = pd.DataFrame({
        "avg_dollar_volume": [1e9, 8e8, 9e8, 1e6, 2e6, 1.5e6],
        "median_spread_pct": [0.01, 0.02, 0.015, 0.5, 0.4, 0.45],
        "realized_vol_annualized_pct": [20.0, 22.0, 21.0, 20.0, 22.0, 21.0],
        "pct_days_trending_adx": [60.0, 65.0, 62.0, 20.0, 25.0, 22.0],
        "hurst": [0.75, 0.70, 0.72, 0.50, 0.52, 0.51],
        "hurst_significant": [True, True, True, False, False, False],
        "hurst_p_value": [0.01, 0.01, 0.01, 0.8, 0.9, 0.85],
        "avg_correlation_to_universe": [0.3, 0.4, 0.35, 0.7, 0.75, 0.72],
        "history_years": [10.0, 8.0, 9.0, 3.0, 2.0, 2.5],
        "sector": ["technology", "technology", "technology", "utilities", "utilities", "utilities"],
    }, index=["S1", "S2", "S3", "S4", "S5", "S6"])

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        scored = score_universe(df.copy(), enable_sector_neutral=True)

    assert len(recorded) == 0, f"Expected no warnings, but caught: {[str(w.message) for w in recorded]}"


# ---------------------------------------------------------------------------
# Task M3.1: opt-in daily-factor supplement (VWAP slope / IVOL / 1M-1W)
# ---------------------------------------------------------------------------
# Frozen default-output column contract, captured from the pre-M3.1 code path.
# If the flag's default ever flips, or the guard stops short-circuiting, this
# list changes and every prior universe-selection result becomes irreproducible.
_EXPECTED_DEFAULT_COLUMNS = [
    "avg_dollar_volume", "median_spread_pct", "realized_vol_annualized_pct",
    "pct_days_trending_adx", "hurst", "hurst_significant", "hurst_p_value",
    "avg_correlation_to_universe", "history_years",
    "liquidity_score", "vol_adequacy_score", "predictability_score",
    "candlestick_score", "momentum_score", "diversification_score",
    "history_adequacy_score", "overall_selection_score",
]

# Exact pre-M3.1 values for `make_metrics()` (captured from the committed
# pre-change module): a column-order check alone would let a numeric regression
# through, so the default path's headline numbers are pinned here too.
_EXPECTED_DEFAULT_OVERALL = {
    "TRENDY": 64.43089430894308,
    "MEANREVY": 26.829268292682926,
    "NEUTRAL": 54.0650406504065,
}


def _daily_frames(trends, n=120, seed=4):
    """Synthetic OHLCV frames per symbol, trending at `trends[symbol]` per day."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    frames = {}
    for symbol, drift in trends.items():
        rets = drift + rng.normal(0.0, 0.004, n)
        close = 100.0 * np.cumprod(1.0 + rets)
        volume = 1e6 * (1.0 + 0.2 * np.sin(np.arange(n) / 5.0))
        frames[symbol] = pd.DataFrame({
            "Open": close, "High": close * 1.005, "Low": close * 0.995,
            "Close": close, "Volume": volume,
        }, index=idx)
    return frames


def test_daily_factors_default_off_is_bit_identical_to_pre_m31_output():
    metrics = make_metrics()
    implicit_off = score_universe(metrics)
    explicit_off = score_universe(metrics, use_daily_factors=False)

    # Same columns, same order -- the frozen pre-M3.1 structure.
    assert list(implicit_off.columns) == _EXPECTED_DEFAULT_COLUMNS
    assert list(explicit_off.columns) == _EXPECTED_DEFAULT_COLUMNS
    pd.testing.assert_frame_equal(implicit_off, explicit_off, atol=0.0, rtol=0.0)
    # ...and the same numbers, not just the same shape.
    for symbol, expected in _EXPECTED_DEFAULT_OVERALL.items():
        assert implicit_off.loc[symbol, "overall_selection_score"] == pytest.approx(expected, rel=0, abs=1e-12)
    # And the input frame was not mutated with stray factor columns.
    assert list(metrics.columns) == [
        "avg_dollar_volume", "median_spread_pct", "realized_vol_annualized_pct",
        "pct_days_trending_adx", "hurst", "hurst_significant", "hurst_p_value",
        "avg_correlation_to_universe", "history_years",
    ]


def test_daily_factors_opt_in_adds_exactly_the_new_columns():
    metrics = make_metrics()
    # Caller-merged raw factor values (the second supported source).
    metrics["vwap_slope"] = [0.004, -0.003, 0.0005]
    metrics["ivol_residual"] = [0.10, 0.35, 0.22]
    metrics["momentum_1m_1w"] = [0.06, -0.04, 0.01]

    off = score_universe(metrics.drop(columns=list(DAILY_FACTOR_RAW_COLUMNS)))
    on = score_universe(metrics, use_daily_factors=True)

    new_columns = set(DAILY_FACTOR_RAW_COLUMNS) | set(DAILY_FACTOR_SCORE_COLUMNS)
    assert set(on.columns) - set(off.columns) == new_columns
    # Every pre-existing column is untouched, including the overall score.
    shared = [c for c in off.columns]
    pd.testing.assert_frame_equal(on[shared], off[shared], atol=0.0, rtol=0.0)

    # Direction sanity: the rising-VWAP symbol gets the top slope score and the
    # high-IVOL symbol the LOWEST ivol score (less diversifiable risk is better).
    assert on["vwap_slope_score"].idxmax() == "TRENDY"
    assert on["ivol_residual_score"].idxmin() == "MEANREVY"
    assert on["momentum_1m_1w_score"].idxmax() == "TRENDY"


def test_daily_factors_opt_in_from_daily_data_panel():
    metrics = make_metrics()
    daily_data = _daily_frames({"TRENDY": 0.002, "MEANREVY": -0.002, "NEUTRAL": 0.0})

    scored = score_universe(metrics, use_daily_factors=True, daily_data=daily_data)

    assert scored.loc["TRENDY", "vwap_slope"] > 0 > scored.loc["MEANREVY", "vwap_slope"]
    assert scored.loc["TRENDY", "momentum_1m_1w"] > scored.loc["NEUTRAL", "momentum_1m_1w"]
    assert scored.loc["MEANREVY", "momentum_1m_1w"] < 0
    # IVOL is vs. the panel's equal-weight mean, so it can only be >= 0.
    assert (scored["ivol_residual"] >= 0).all()


def test_daily_factors_only_enter_the_score_when_the_caller_supplies_weights():
    metrics = make_metrics()
    daily_data = _daily_frames({"TRENDY": 0.002, "MEANREVY": -0.002, "NEUTRAL": 0.0})

    off = score_universe(metrics)
    without = score_universe(metrics, use_daily_factors=True, daily_data=daily_data)
    with_weights = score_universe(
        metrics,
        use_daily_factors=True,
        daily_data=daily_data,
        weights={**DEFAULT_WEIGHTS, **DAILY_FACTOR_WEIGHTS},
    )

    # Merely enabling the flag must NOT move the overall score: DEFAULT_WEIGHTS is
    # never silently extended, so the factors are attached but not weighted.
    pd.testing.assert_series_equal(without["overall_selection_score"],
                                   off["overall_selection_score"])
    # With the explicit budget they do participate ...
    assert not without["overall_selection_score"].equals(with_weights["overall_selection_score"])
    # ... while every pre-existing component column is still byte-for-byte equal.
    factor_columns = set(DAILY_FACTOR_SCORE_COLUMNS) | set(DAILY_FACTOR_RAW_COLUMNS)
    for column in without.columns:
        if column in factor_columns or column == "overall_selection_score":
            continue
        pd.testing.assert_series_equal(without[column], with_weights[column])


def test_daily_factors_requires_a_source_when_enabled():
    with pytest.raises(ValueError, match="use_daily_factors=True needs a factor source"):
        score_universe(make_metrics(), use_daily_factors=True)


def test_daily_factors_partial_source_columns_are_rejected():
    metrics = make_metrics()
    metrics["vwap_slope"] = [0.004, -0.003, 0.0005]
    with pytest.raises(ValueError, match="partial daily-factor columns"):
        score_universe(metrics, use_daily_factors=True)


def test_daily_factors_symbol_missing_from_daily_data_stays_nan():
    metrics = make_metrics()
    daily_data = _daily_frames({"TRENDY": 0.002, "MEANREVY": -0.002})  # NEUTRAL absent

    scored = score_universe(metrics, use_daily_factors=True, daily_data=daily_data)

    assert np.isnan(scored.loc["NEUTRAL", "vwap_slope"])
    assert np.isnan(scored.loc["NEUTRAL", "momentum_1m_1w"])
    # NaN must stay NaN in the score too (excluded from ranking, never 0.0).
    assert np.isnan(scored.loc["NEUTRAL", "vwap_slope_score"])
    assert np.isnan(scored.loc["NEUTRAL", "ivol_residual_score"])
    assert scored.loc["TRENDY", "vwap_slope_score"] == pytest.approx(100.0)


def test_daily_factors_invalid_daily_data_frame_is_rejected():
    metrics = make_metrics()
    bad = {"TRENDY": pd.DataFrame({"Close": [1.0, 2.0]})}  # no Volume column
    with pytest.raises(ValueError, match="missing column"):
        score_universe(metrics, use_daily_factors=True, daily_data=bad)

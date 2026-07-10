import numpy as np
import pandas as pd
import pytest

from selectorbot.scoring import score_universe


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


def test_etf_expense_score_favors_lower_expense_ratio():
    metrics_with_meta = make_metrics(expense_ratio=[0.001, 0.02, 0.005], total_assets=[1e11, 1e8, 5e9])
    scored = score_universe(metrics_with_meta)
    assert scored["etf_expense_score"].idxmax() == "TRENDY"  # lowest expense ratio (0.001)


def test_overall_score_weighted_average_matches_manual_calculation_when_all_present():
    metrics_with_meta = make_metrics(expense_ratio=[0.001, 0.02, 0.005], total_assets=[1e11, 1e8, 5e9])
    scored = score_universe(metrics_with_meta)
    weights = {
        "liquidity_score": 0.30, "vol_adequacy_score": 0.20, "predictability_score": 0.20,
        "diversification_score": 0.15, "history_adequacy_score": 0.10,
        "etf_expense_score": 0.025, "etf_aum_score": 0.025,
    }
    row = scored.loc["TRENDY"]
    expected = sum(row[col] * w for col, w in weights.items())
    assert row["overall_selection_score"] == pytest.approx(expected)

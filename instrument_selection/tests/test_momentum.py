import numpy as np
import pandas as pd
import pytest
from common.indicators import macd, roc
from common.testing import make_ar1_ohlcv

from selectorbot.config import SelectionConfig
from selectorbot.momentum import _corr, momentum_efficacy, momentum_summary
from selectorbot.scoring import score_universe


# --- indicator math ------------------------------------------------------

def test_roc_matches_trailing_return():
    close = pd.Series([10, 11, 12, 15, 20, 25], dtype=float)
    result = roc(close, period=3)
    assert result.iloc[3] == pytest.approx(15 / 10 - 1)   # 3 bars earlier (index 0) was 10
    assert result.iloc[5] == pytest.approx(25 / 12 - 1)   # 3 bars earlier (index 2) was 12
    assert np.isnan(result.iloc[2])                        # not enough history yet


def test_macd_positive_in_uptrend_and_zero_when_flat():
    up = pd.Series(np.linspace(100, 200, 300))
    flat = pd.Series(np.full(300, 100.0))
    up_macd = macd(up).dropna()
    flat_macd = macd(flat).dropna()
    assert up_macd["macd"].iloc[-1] > 0            # fast EMA above slow EMA in a rising series
    assert flat_macd["macd"].iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_corr_guards_degenerate_input():
    assert np.isnan(_corr(np.array([1.0, 1.0, 1.0]), np.array([1.0, 2.0, 3.0])))  # zero variance
    assert np.isnan(_corr(np.array([1.0]), np.array([2.0])))                      # too few points


# --- efficacy significance test (mirrors the Hurst/candlestick conventions) ---

def test_momentum_efficacy_flags_positive_autocorrelation_as_momentum():
    df = make_ar1_ohlcv(phi=0.7, n=3000, seed=1)      # strongly, positively autocorrelated returns
    result = momentum_efficacy(df["Close"], lookback=5, horizon=5, seed=0)
    assert result["momentum_edge"] > 0
    assert result["momentum_significant"]


def test_momentum_efficacy_detects_mean_reversion_as_negative_edge():
    df = make_ar1_ohlcv(phi=-0.7, n=3000, seed=2)     # negatively autocorrelated returns
    result = momentum_efficacy(df["Close"], lookback=5, horizon=5, seed=0)
    assert result["momentum_edge"] < 0
    assert result["momentum_significant"]


def test_momentum_efficacy_does_not_flag_a_random_walk():
    df = make_ar1_ohlcv(phi=0.0, n=3000, seed=3)      # iid returns -> no serial structure
    result = momentum_efficacy(df["Close"], lookback=5, horizon=5, seed=0)
    assert not result["momentum_significant"]


def test_momentum_efficacy_reports_insufficient_history():
    df = make_ar1_ohlcv(phi=0.5, n=50, seed=4)
    result = momentum_efficacy(df["Close"], lookback=252, horizon=21, seed=0)
    assert np.isnan(result["momentum_edge"])
    assert result["momentum_significant"] is False


# --- summary -------------------------------------------------------------

def test_momentum_summary_labels_a_trending_series_as_momentum():
    df = make_ar1_ohlcv(phi=0.7, n=3000, seed=5)
    config = SelectionConfig(momentum_lookback=5, momentum_horizon=5,
                             momentum_min_obs=100, momentum_trend_ma=20)
    result = momentum_summary(df, config)
    assert result["momentum_label"] == "momentum"
    assert result["momentum_significant"]
    assert not np.isnan(result["momentum_lookback_return"])
    assert 0.0 <= result["pct_days_above_trend_ma"] <= 100.0


def test_momentum_summary_reports_insufficient_data_for_short_series():
    df = make_ar1_ohlcv(phi=0.5, n=100, seed=6)
    config = SelectionConfig()  # default momentum_min_obs = 400
    result = momentum_summary(df, config)
    assert result["momentum_label"] == "insufficient_data"
    assert np.isnan(result["momentum_edge"])
    assert result["momentum_significant"] is False


# --- scoring integration -------------------------------------------------

def test_momentum_score_gates_on_significance():
    metrics = pd.DataFrame({
        "avg_dollar_volume": [1e7, 1e7],
        "median_spread_pct": [0.01, 0.01],
        "realized_vol_annualized_pct": [20.0, 20.0],
        "hurst": [0.5, 0.5],
        "hurst_significant": [False, False],
        "momentum_edge": [0.2, 0.2],              # identical magnitude...
        "momentum_significant": [True, False],    # ...but only one is significant
        "history_years": [10.0, 10.0],
    }, index=["SIG", "NOISE"])
    scored = score_universe(metrics)
    assert scored.loc["SIG", "momentum_score"] > scored.loc["NOISE", "momentum_score"]


def test_missing_momentum_edge_does_not_penalize_overall_score():
    metrics = pd.DataFrame({
        "avg_dollar_volume": [1e7, 1e7],
        "median_spread_pct": [0.01, 0.01],
        "realized_vol_annualized_pct": [20.0, 20.0],
        "hurst": [0.6, 0.6],
        "hurst_significant": [True, True],
        "momentum_edge": [0.2, np.nan],           # one symbol has no momentum data
        "momentum_significant": [True, False],
        "history_years": [10.0, 10.0],
    }, index=["HAS_IT", "MISSING"])
    scored = score_universe(metrics)
    assert np.isnan(scored.loc["MISSING", "momentum_score"])
    assert scored.loc["MISSING", "overall_selection_score"] > 0  # weight renormalized, not penalized
    assert not np.isnan(scored.loc["MISSING", "overall_selection_score"])

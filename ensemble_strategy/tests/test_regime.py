import numpy as np
import pandas as pd

from ensemblebot.config import EnsembleConfig
from ensemblebot.regime import apply_hysteresis, classify_regime


def test_hysteresis_holds_previous_state_in_dead_zone():
    adx = pd.Series([30.0, 30.0, 22.0, 22.0, 22.0, 15.0, 22.0, 22.0])
    result = apply_hysteresis(adx, trend_threshold=25.0, range_threshold=20.0)
    assert list(result) == ["trend", "trend", "trend", "trend", "trend", "range", "range", "range"]


def test_hysteresis_defaults_to_range_before_any_threshold_crossed():
    adx = pd.Series([22.0, 23.0, 24.0])  # never crosses either threshold
    result = apply_hysteresis(adx, trend_threshold=25.0, range_threshold=20.0)
    assert list(result) == ["range", "range", "range"]


def make_df(closes, n_pad=250):
    closes = pd.Series(np.concatenate([np.full(n_pad, closes[0]), closes]), dtype=float)
    idx = pd.bdate_range("2018-01-01", periods=len(closes))
    return pd.DataFrame({
        "Open": closes.values, "High": closes.values + 0.5, "Low": closes.values - 0.5, "Close": closes.values,
    }, index=idx)


def test_downtrend_overrides_adx_even_if_trending():
    # A strong, sustained decline: ADX will read as trending, but the
    # long-term 200-day filter must still classify this as "downtrend", not "trend".
    decline = 100 - np.arange(300) * 0.3
    df = make_df(decline)
    config = EnsembleConfig()
    classified = classify_regime(df, config)
    tail = classified.iloc[-30:]
    assert (tail["regime"] == "downtrend").all()


def test_regime_is_shifted_and_has_no_lookahead():
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    df_full = make_df(closes)
    config = EnsembleConfig()

    cutoff = len(df_full) - 20
    df_truncated = df_full.iloc[:cutoff]

    classified_full = classify_regime(df_full, config)
    classified_truncated = classify_regime(df_truncated, config)

    # Regime values up to the truncation point must be identical whether or
    # not future data exists -- proves no lookahead into not-yet-seen bars.
    common = classified_truncated.index[:-1]  # drop the last bar, which depends on the truncated series' own tail
    pd.testing.assert_series_equal(
        classified_full.loc[common, "regime"], classified_truncated.loc[common, "regime"], check_names=False
    )

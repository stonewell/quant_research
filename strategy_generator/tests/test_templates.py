import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes as make_df

from stratgen.indicators import realized_vol, roc, rsi, sma
from stratgen.templates import (
    AbsoluteMomentumTemplate,
    MeanReversionTemplate,
    MomentumTemplate,
    NoTradeTemplate,
    TurnOfMonthTemplate,
    VolGatedMomentumTemplate,
)


def test_momentum_template_matches_manual_ma_state():
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    df = make_df(closes)
    template = MomentumTemplate()
    params = {"fast_ma": 20, "slow_ma": 100}

    result = template.signals(df, params)
    fast, slow = sma(df["Close"], 20), sma(df["Close"], 100)
    expected_entry = (fast > slow).fillna(False)
    pd.testing.assert_series_equal(result["entry_signal"], expected_entry, check_names=False)
    pd.testing.assert_series_equal(result["exit_signal"], (~expected_entry).fillna(False), check_names=False)


def test_mean_reversion_template_matches_manual_rsi_thresholds():
    rng = np.random.default_rng(2)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    df = make_df(closes)
    template = MeanReversionTemplate(rsi_period=2)
    params = {"entry_threshold": 10, "exit_threshold": 70}

    result = template.signals(df, params)
    r = rsi(df["Close"], 2)
    pd.testing.assert_series_equal(result["entry_signal"], (r < 10).fillna(False), check_names=False)
    pd.testing.assert_series_equal(result["exit_signal"], (r > 70).fillna(False), check_names=False)


def test_no_trade_template_never_signals():
    df = make_df(100 + np.cumsum(np.random.default_rng(3).normal(0, 1, 200)))
    result = NoTradeTemplate().signals(df, {})
    assert not result["entry_signal"].any()
    assert not result["exit_signal"].any()


def test_momentum_and_meanrev_param_grids_are_small():
    # Deliberately constrained search space (2 free params each), per the
    # research-documented mitigation against unconstrained/GP-style search.
    assert len(MomentumTemplate().param_grid) == 2
    assert len(MeanReversionTemplate().param_grid) == 2


def test_turn_of_month_template_matches_manual_calendar_calc():
    idx = pd.bdate_range("2021-01-01", periods=400)
    closes = 100 + np.cumsum(np.random.default_rng(11).normal(0, 1, len(idx)))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    template = TurnOfMonthTemplate()
    params = {"entry_days_before_month_end": 2, "exit_trading_day_of_month": 3}

    result = template.signals(df, params)

    month_key = pd.Series(idx.to_period("M"), index=idx)
    day_rank = month_key.groupby(month_key).cumcount() + 1
    days_in_month = month_key.groupby(month_key).transform("size")
    days_until_end = days_in_month - day_rank
    expected_entry = days_until_end < 2
    expected_exit = day_rank > 3

    pd.testing.assert_series_equal(result["entry_signal"], expected_entry, check_names=False)
    pd.testing.assert_series_equal(result["exit_signal"], expected_exit, check_names=False)
    # Entry should only ever fire in the last couple of trading days of a month.
    assert (days_in_month[result["entry_signal"]] - day_rank[result["entry_signal"]] < 2).all()


def test_turn_of_month_template_trades_only_a_few_days_per_month():
    idx = pd.bdate_range("2021-01-01", periods=500)
    closes = 100 + np.cumsum(np.random.default_rng(12).normal(0, 1, len(idx)))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    template = TurnOfMonthTemplate()
    result = template.signals(df, {"entry_days_before_month_end": 1, "exit_trading_day_of_month": 3})
    # Roughly 1 entry day and up to 3 exit-eligible days per ~21-trading-day month --
    # entry should fire far less often than it doesn't.
    assert 0 < result["entry_signal"].mean() < 0.15


def test_vol_gated_momentum_template_matches_manual_calc():
    rng = np.random.default_rng(13)
    closes = 100 + np.cumsum(rng.normal(0, 1, 500))
    df = make_df(closes)
    template = VolGatedMomentumTemplate()
    params = {"vol_lookback": 20, "vol_percentile": 90}

    result = template.signals(df, params)

    trend_ma = sma(df["Close"], template.trend_ma_period)
    trend_ok = (df["Close"] > trend_ma).fillna(False)
    vol = realized_vol(df["Close"], 20)
    vol_threshold = vol.rolling(252, min_periods=252).quantile(0.90)
    vol_extreme_high = (vol > vol_threshold).fillna(False)

    pd.testing.assert_series_equal(result["entry_signal"], (trend_ok & ~vol_extreme_high), check_names=False)
    pd.testing.assert_series_equal(result["exit_signal"], ((~trend_ok) | vol_extreme_high), check_names=False)


def test_vol_gated_momentum_template_blocks_entries_during_a_vol_spike():
    # A quiet uptrend (trend_ok true throughout) with one deterministic, sharp
    # volatility spike injected midway -- entries should be blocked and any
    # open position forced flat only during/around that spike, not the whole
    # otherwise-calm uptrend.
    n = 500
    idx = pd.bdate_range("2019-01-01", periods=n)
    t = np.arange(n)
    closes = 100 + t * 0.05
    closes = closes.astype(float)
    rng = np.random.default_rng(14)
    spike = np.zeros(n)
    spike[300:320] = rng.normal(0, 8, 20)  # a short, violent volatility burst
    closes = closes + spike
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)

    template = VolGatedMomentumTemplate()
    result = template.signals(df, {"vol_lookback": 10, "vol_percentile": 90})

    assert result["entry_signal"].iloc[:280].any()  # normal calm uptrend: entries allowed
    assert result["exit_signal"].iloc[305:320].any()  # spike window: forced flat at some point
    assert len(template.param_grid) == 2


def test_new_calendar_and_vol_gated_param_grids_are_small():
    assert len(TurnOfMonthTemplate().param_grid) == 2
    assert len(VolGatedMomentumTemplate().param_grid) == 2


def test_absolute_momentum_template_matches_manual_roc_signs():
    rng = np.random.default_rng(21)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 500)))
    df = make_df(closes)
    template = AbsoluteMomentumTemplate()
    params = {"entry_lookback": 252, "exit_lookback": 21}

    result = template.signals(df, params)

    expected_entry = (roc(df["Close"], 252) > 0).fillna(False)
    expected_exit = (roc(df["Close"], 21) < 0).fillna(False)
    pd.testing.assert_series_equal(result["entry_signal"], expected_entry, check_names=False)
    pd.testing.assert_series_equal(result["exit_signal"], expected_exit, check_names=False)


def test_absolute_momentum_holds_a_steady_uptrend_and_exits_a_downtrend():
    # A clean uptrend for the whole entry-lookback window should keep the
    # trailing return positive (entry on, exit off); a clean downtrend should
    # flip both -- the core "hold your own trend, step to cash otherwise" rule.
    n = 400
    up = make_df(100 * np.exp(np.cumsum(np.full(n, 0.002))))     # monotonic up
    down = make_df(100 * np.exp(np.cumsum(np.full(n, -0.002))))  # monotonic down
    template = AbsoluteMomentumTemplate()
    params = {"entry_lookback": 126, "exit_lookback": 21}

    up_sig = template.signals(up, params)
    down_sig = template.signals(down, params)

    # After warmup, an unbroken uptrend is fully invested and never exit-signaled...
    assert up_sig["entry_signal"].iloc[200:].all()
    assert not up_sig["exit_signal"].iloc[200:].any()
    # ...and an unbroken downtrend is the mirror image.
    assert not down_sig["entry_signal"].iloc[200:].any()
    assert down_sig["exit_signal"].iloc[200:].all()


def test_absolute_momentum_param_grid_is_small():
    assert len(AbsoluteMomentumTemplate().param_grid) == 2

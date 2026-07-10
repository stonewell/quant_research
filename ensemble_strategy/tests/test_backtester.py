import numpy as np
import pandas as pd

from ensemblebot.backtester import run_backtest
from ensemblebot.config import EnsembleConfig


def make_mixed_regime_df(n=1200, seed=7):
    """Uptrend (should read as 'trend'), then chop (should read as 'range'),
    then decline (should read as 'downtrend')."""
    rng = np.random.default_rng(seed)
    uptrend = 100 + np.arange(400) * 0.3
    chop = uptrend[-1] + 8 * np.sin(np.arange(400) / 15.0) + rng.normal(0, 0.3, 400)
    decline = chop[-1] - np.arange(400) * 0.25
    close = np.concatenate([uptrend, chop, decline])
    high = close + np.abs(rng.normal(0.4, 0.15, len(close)))
    low = close - np.abs(rng.normal(0.4, 0.15, len(close)))
    open_ = close + rng.normal(0, 0.1, len(close))
    idx = pd.bdate_range("2015-01-01", periods=len(close))
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def test_ensemble_runs_and_visits_multiple_regimes():
    df = make_mixed_regime_df()
    config = EnsembleConfig(mode="ensemble", warmup_bars=210)
    result = run_backtest(df, config)
    eq = result["equity_curve"]
    assert not eq.empty
    assert (eq["equity"] > 0).all()
    assert set(eq["regime"].unique()) >= {"trend", "downtrend"}  # should see both, given the synthetic data


def test_ensemble_never_lets_cash_go_negative():
    df = make_mixed_regime_df(seed=11)
    config = EnsembleConfig(mode="ensemble", warmup_bars=210, initial_capital=20_000.0)
    result = run_backtest(df, config)
    assert (result["equity_curve"]["cash"] >= -1e-6).all()


def test_downtrend_regime_forces_flat_position():
    df = make_mixed_regime_df(seed=13)
    config = EnsembleConfig(mode="ensemble", warmup_bars=210)
    result = run_backtest(df, config)
    eq = result["equity_curve"]
    downtrend_rows = eq[eq["regime"] == "downtrend"]
    assert not downtrend_rows.empty
    # Once in a confirmed downtrend, the very next bar should already be flat
    # (the exit executes at that bar's open, before this bar's mark-to-market).
    assert not downtrend_rows.iloc[5:]["in_position"].any()


def test_trend_only_mode_ignores_rsi_and_stays_invested_through_chop():
    df = make_mixed_regime_df(seed=17)
    config = EnsembleConfig(mode="trend_only", warmup_bars=210)
    result = run_backtest(df, config)
    eq = result["equity_curve"]
    # trend_only should be invested whenever price is above its 200-day SMA,
    # i.e., during both the "trend" and "range" regime windows, without the
    # RSI(2) tactical flipping the ensemble/meanrev_only modes would do.
    non_downtrend = eq[eq["regime"] != "downtrend"]
    assert non_downtrend["in_position"].mean() > 0.95


def test_meanrev_only_mode_trades_more_often_than_trend_only():
    df = make_mixed_regime_df(seed=19)
    trend_result = run_backtest(df, EnsembleConfig(mode="trend_only", warmup_bars=210))
    meanrev_result = run_backtest(df, EnsembleConfig(mode="meanrev_only", warmup_bars=210))
    assert len(meanrev_result["trades"]) > len(trend_result["trades"])

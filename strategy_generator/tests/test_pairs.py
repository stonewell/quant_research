import numpy as np
import pandas as pd

from stratgen.pairs import PairsConfig, pairs_signals, spread_zscore
from stratgen.pairs_backtester import run_pairs_backtest


def _make_pair(n=200, bump_start=90, bump_len=6, bump_size=5.0, revert_len=14, seed=1):
    """Two price series tracking a common level with small independent
    Gaussian noise (seeded, so the rolling spread has a small nonzero std
    without ever hitting exactly zero) plus a large, deterministic
    divergence-then-convergence bump injected into `a`. The bump is ~100x
    the noise amplitude, so entry-threshold crossings are reliably driven by
    the bump alone, not by chance noise wandering -- the same
    "engineer a deterministic trigger, on top of realistic noise" style
    test_backtester.py's stop-loss test uses."""
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(seed)
    common = 100 + np.arange(n, dtype=float) * 0.01
    a = common + rng.normal(0, 0.05, n)
    b = common + rng.normal(0, 0.05, n)

    if bump_start + bump_len <= n:
        a[bump_start:bump_start + bump_len] += bump_size
    revert_end = min(bump_start + bump_len + revert_len, n)
    revert_actual_len = revert_end - (bump_start + bump_len)
    if revert_actual_len > 0:
        a[bump_start + bump_len:revert_end] += np.linspace(bump_size, 0, revert_len)[:revert_actual_len]

    df_a = pd.DataFrame({"Open": a, "High": a + 0.05, "Low": a - 0.05, "Close": a}, index=idx)
    df_b = pd.DataFrame({"Open": b, "High": b + 0.05, "Low": b - 0.05, "Close": b}, index=idx)
    return df_a, df_b


def test_spread_zscore_matches_manual_rolling_calc():
    df_a, df_b = _make_pair()
    z = spread_zscore(df_a["Close"], df_b["Close"], lookback=30)

    log_spread = np.log(df_a["Close"]) - np.log(df_b["Close"])
    mean = log_spread.rolling(30, min_periods=30).mean()
    std = log_spread.rolling(30, min_periods=30).std()
    expected = (log_spread - mean) / std.replace(0, np.nan)
    pd.testing.assert_series_equal(z, expected, check_names=False)


def test_pairs_signals_fire_in_correct_direction_on_deterministic_divergence():
    df_a, df_b = _make_pair(bump_start=90, bump_len=6, bump_size=5.0)
    # entry_zscore=3.0 here (vs. the module default of 2.0) to stay clear of a
    # couple of noise-driven |z|>2 crossings this seed's baseline happens to
    # produce elsewhere -- rolling z-scores of overlapping-window noise are
    # not i.i.d., so occasional chance crossings are expected and not a bug;
    # the deliberate bump (z up to ~5.3) clears 3.0 by a wide margin regardless.
    config = PairsConfig(lookback=30, entry_zscore=3.0, exit_zscore=0.5)
    sig = pairs_signals(df_a, df_b, config)

    # `a` was pushed sharply ABOVE `b` -> a is "rich" -> short a, long b.
    assert sig["enter_short_a_long_b"].iloc[90:96].any()
    assert not sig["enter_long_a_short_b"].any()

    # After the bump fully reverts and flushes out of the rolling lookback
    # window, the spread converges back near zero and exit_signal fires.
    assert sig["exit_signal"].iloc[131:150].any()


def test_pairs_signals_fire_in_opposite_direction_when_a_is_cheap():
    df_a, df_b = _make_pair(bump_start=90, bump_len=6, bump_size=-5.0)
    config = PairsConfig(lookback=30, entry_zscore=2.0)
    sig = pairs_signals(df_a, df_b, config)

    assert sig["enter_long_a_short_b"].iloc[90:96].any()
    assert not sig["enter_short_a_long_b"].iloc[80:140].any()


def test_pairs_backtest_opens_correct_direction_and_profits_on_convergence():
    df_a, df_b = _make_pair(bump_start=90, bump_len=6, bump_size=5.0, revert_len=14)
    # entry_zscore=3.0: see comment in test_pairs_signals_fire_in_correct_direction_
    # on_deterministic_divergence -- keeps this test isolated to the deliberate bump.
    config = PairsConfig(lookback=30, entry_zscore=3.0, exit_zscore=0.5, max_holding_days=60)
    result = run_pairs_backtest(df_a, df_b, config, warmup=40)

    trades = result["trades"]
    entries = trades[trades["event"] == "entry"]
    assert not entries.empty
    assert set(entries["side"]) == {"buy", "short"}
    # `a` was pushed rich -> short leg should be on instrument "a", long leg on "b".
    assert (entries.loc[entries["side"] == "short", "instrument"] == "a").all()
    assert (entries.loc[entries["side"] == "buy", "instrument"] == "b").all()

    exits = trades[trades["event"] != "entry"]
    assert not exits.empty
    # The spread fully reverted -- net P&L across both legs of the closed trade(s) should be positive.
    total_pnl = exits["pnl"].sum()
    assert total_pnl > 0

    assert (result["equity_curve"]["equity"] > 0).all()


def test_pairs_backtest_max_holding_days_forces_exit_when_spread_never_converges():
    idx_n = 200
    idx = pd.bdate_range("2020-01-01", periods=idx_n)
    rng = np.random.default_rng(2)
    common = 100 + np.arange(idx_n, dtype=float) * 0.01
    a = common + rng.normal(0, 0.05, idx_n)
    b = common + rng.normal(0, 0.05, idx_n)
    a[90:] += 5.0  # permanent step divergence -- never reverts within the test window
    df_a = pd.DataFrame({"Open": a, "High": a + 0.05, "Low": a - 0.05, "Close": a}, index=idx)
    df_b = pd.DataFrame({"Open": b, "High": b + 0.05, "Low": b - 0.05, "Close": b}, index=idx)

    config = PairsConfig(lookback=30, entry_zscore=2.0, exit_zscore=0.5, stop_zscore=100.0, max_holding_days=5)
    result = run_pairs_backtest(df_a, df_b, config, warmup=40)

    trades = result["trades"]
    entry_dates = trades[trades["event"] == "entry"]["date"].unique()
    exit_dates = trades[trades["event"] != "entry"]["date"].unique()
    assert len(entry_dates) > 0
    assert len(exit_dates) > 0

    entry_idx = df_a.index.get_indexer([entry_dates[0]])[0]
    exit_idx = df_a.index.get_indexer([exit_dates[0]])[0]
    # Position must be closed no later than max_holding_days bars after entry,
    # even though the spread itself never converges in this window.
    assert exit_idx - entry_idx <= config.max_holding_days + 1


def test_pairs_backtest_equity_always_stays_positive():
    df_a, df_b = _make_pair(bump_start=90, bump_len=6, bump_size=5.0)
    config = PairsConfig(lookback=30, entry_zscore=2.0, exit_zscore=0.5)
    result = run_pairs_backtest(df_a, df_b, config, warmup=40)
    # Cash alone can legitimately look low/negative-ish mid-trade since the
    # short leg's liability isn't cash -- but overall equity (cash + long
    # value - short liability) must always stay positive.
    assert (result["equity_curve"]["equity"] > 0).all()


def test_pairs_backtest_raises_when_not_enough_bars_for_warmup():
    df_a, df_b = _make_pair(n=30, bump_start=10, bump_len=2, revert_len=3)
    config = PairsConfig(lookback=30)
    try:
        run_pairs_backtest(df_a, df_b, config, warmup=40)
        assert False, "expected ValueError"
    except ValueError:
        pass

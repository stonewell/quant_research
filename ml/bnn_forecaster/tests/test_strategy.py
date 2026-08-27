"""Unit tests for bnnf/strategy.py's BnnForecastStrategy.

Most tests mock `fit_forecast` with controlled forecast_return/ci_width
series -- a real AutoBNN fit costs several seconds even at the smallest
practical settings (JAX JIT compilation dominates), and its actual
calibration quality is a separate, disclosed-as-unverified concern (see
bnnf/forecasting.py's own docstring) that unit tests for the STRATEGY's
entry/exit/aggregation mechanics shouldn't depend on. One slow end-to-end
test (`test_generate_weights_runs_a_real_fit_without_crashing`) exercises
the real fit path directly, only checking shape/no-crash, not calibration.
"""

import os
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_BNN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BNN_ROOT not in sys.path:
    sys.path.insert(0, _BNN_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.testing import make_ohlcv_from_closes
from bnnf.config import ForecasterConfig
from bnnf.strategy import BnnForecastStrategy


def _linear_universe(n=300, drifts=None):
    drifts = drifts or {}
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    universe = {}
    for sym, drift in drifts.items():
        df = make_ohlcv_from_closes(100.0 + drift * t)
        df.index = dates
        universe[sym] = df
    return universe


def _fake_forecast(universe, table):
    """`table`: symbol -> constant (forecast_return, ci_width) applied
    across the whole index -- enough to test entry/exit/aggregation
    mechanics without a real (slow, uncalibrated-by-default) BNN fit.
    Matches the `close` Series `fit_forecast` was actually called with
    against each candidate's known series by value (a DataFrame column
    slice's own `.name` is always "Close", not the symbol, so that can't be
    used to tell candidates apart)."""
    def _fetch(close, cfg):
        for sym, df in universe.items():
            if close.equals(df["Close"]):
                forecast_return, ci_width = table.get(sym, (float("nan"), float("nan")))
                return pd.DataFrame(
                    {"forecast_return": forecast_return, "ci_width": ci_width}, index=close.index,
                )
        raise AssertionError("fit_forecast called with a close series not in the test's universe")
    return _fetch


def test_strategy_holds_confident_symbol_that_clears_the_hurdle():
    universe = _linear_universe(drifts={"KO": 0.20, "SPY": 0.05, "BIL": 0.0})
    cfg = ForecasterConfig(universe=["KO"], benchmark_symbol="SPY", required_return=0.10, max_ci_width=0.30)
    strat = BnnForecastStrategy(cfg)

    table = {"KO": (0.20, 0.10), "SPY": (0.03, 0.10)}
    with patch("bnnf.strategy.fit_forecast", side_effect=_fake_forecast(universe, table)):
        weights = strat.generate_weights(universe)

    daily = weights.ffill().fillna(0.0)
    assert (daily["KO"] > 0).any()


def test_strategy_never_holds_unconfident_symbol_despite_great_forecast():
    universe = _linear_universe(drifts={"KO": 0.20, "SPY": 0.05, "BIL": 0.0})
    cfg = ForecasterConfig(universe=["KO"], benchmark_symbol="SPY", required_return=0.10, max_ci_width=0.30)
    strat = BnnForecastStrategy(cfg)

    table = {"KO": (0.50, 0.90), "SPY": (0.03, 0.10)}  # KO's forecast is great but interval is too wide
    with patch("bnnf.strategy.fit_forecast", side_effect=_fake_forecast(universe, table)):
        weights = strat.generate_weights(universe)

    daily = weights.ffill().fillna(0.0)
    assert (daily["KO"] == 0).all()


def test_strategy_exits_once_benchmark_forecast_catches_up():
    universe = _linear_universe(drifts={"KO": 0.20, "SPY": 0.05, "BIL": 0.0})
    cfg = ForecasterConfig(universe=["KO"], benchmark_symbol="SPY", required_return=0.05, max_ci_width=0.30)
    strat = BnnForecastStrategy(cfg)

    n = len(universe["KO"])
    ko_return = pd.Series(0.20, index=universe["KO"].index)
    # SPY's own forecast starts low then ramps above KO's constant 0.20 partway through.
    spy_return = pd.Series(
        np.concatenate([np.full(n // 2, 0.02), np.linspace(0.02, 0.40, n - n // 2)]),
        index=universe["SPY"].index,
    )
    forecasts = {"KO": (ko_return, 0.10), "SPY": (spy_return, 0.10)}

    def fake_fit(close, cfg_):
        for sym, df in universe.items():
            if close.equals(df["Close"]):
                ret, ci = forecasts[sym]
                return pd.DataFrame({"forecast_return": ret, "ci_width": ci}, index=close.index)
        raise AssertionError("unrecognized close series")

    with patch("bnnf.strategy.fit_forecast", side_effect=fake_fit):
        weights = strat.generate_weights(universe)

    daily = weights.ffill().fillna(0.0)
    assert (daily["KO"].iloc[:n // 4] > 0).any(), "should be held early while KO's forecast beats SPY's"
    assert (daily["KO"].iloc[-10:] == 0).all(), "should exit once SPY's forecast catches up to KO's"


def test_strategy_returns_empty_when_no_candidates_in_universe():
    universe = _linear_universe(drifts={"SPY": 0.05, "BIL": 0.0})
    strat = BnnForecastStrategy(ForecasterConfig(universe=["KO", "PG"]))
    assert strat.generate_weights(universe).empty


def test_generate_weights_honors_params_overrides_not_just_config_defaults():
    # Regression class already caught once in fundamental_screener this
    # session: a backtester-reconstructed instance is always
    # BnnForecastStrategy() (zero-arg, default config) -- generate_weights
    # must still honor screener-tuned thresholds saved in strategy.json's
    # `params`, not silently fall back to class defaults.
    universe = _linear_universe(drifts={"KO": 0.20, "SPY": 0.05, "BIL": 0.0})
    strat = BnnForecastStrategy()  # default required_return=0.10

    table = {"KO": (0.05, 0.10), "SPY": (0.02, 0.10)}  # 0.05 fails the default 0.10 hurdle
    with patch("bnnf.strategy.fit_forecast", side_effect=_fake_forecast(universe, table)):
        default_weights = strat.generate_weights(universe, {"universe": ["KO"], "benchmark_symbol": "SPY"})
        assert (default_weights.ffill().fillna(0.0)["KO"] == 0).all()

        relaxed_weights = strat.generate_weights(
            universe, {"universe": ["KO"], "benchmark_symbol": "SPY", "required_return": 0.01},
        )
    assert (relaxed_weights.ffill().fillna(0.0)["KO"] > 0).any()


def test_generate_weights_caches_forecasts_across_calls_on_same_instance():
    universe = _linear_universe(drifts={"KO": 0.20, "SPY": 0.05, "BIL": 0.0})
    cfg = ForecasterConfig(universe=["KO"], benchmark_symbol="SPY")
    strat = BnnForecastStrategy(cfg)

    table = {"KO": (0.20, 0.10), "SPY": (0.03, 0.10)}
    with patch("bnnf.strategy.fit_forecast", side_effect=_fake_forecast(universe, table)) as mock_fit:
        strat.generate_weights(universe)
        strat.generate_weights(universe)
        strat.generate_weights(universe)

    assert mock_fit.call_count == 2, "one fit per distinct symbol (KO, SPY), reused across repeated calls"


def test_warmup_bars_equals_lookback_days():
    strat = BnnForecastStrategy(ForecasterConfig(lookback_days=500))
    assert strat.warmup_bars() == 500


def test_explain_weights_mentions_the_disclosed_limitations():
    strat = BnnForecastStrategy()
    explanation = strat.explain_weights()
    assert "DISCLOSED LIMITATION" in explanation
    assert "AutoBNN" in explanation


@pytest.mark.slow
def test_generate_weights_runs_a_real_fit_without_crashing():
    """No mocking -- exercises the real AutoBNN fit/predict path end-to-end
    on tiny synthetic data with the smallest practical settings. Only checks
    shape/no-crash, NOT calibration quality (see bnnf/forecasting.py's own
    docstring for why that's a separate, disclosed-as-unverified concern)."""
    universe = _linear_universe(n=80, drifts={"KO": 0.10, "SPY": 0.02, "BIL": 0.0})
    cfg = ForecasterConfig(
        universe=["KO"], benchmark_symbol="SPY", lookback_days=60, horizon_days=10,
        width=4, num_iters=20, num_particles=1, required_return=-10.0, max_ci_width=1e6,
    )
    strat = BnnForecastStrategy(cfg)
    weights = strat.generate_weights(universe)
    assert not weights.empty

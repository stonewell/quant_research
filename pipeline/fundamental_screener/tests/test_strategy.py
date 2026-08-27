"""Unit tests for fscreen/strategy.py's FundamentalMarginOfSafetyStrategy.
All fundamentals are mocked -- no real network access, matching this
workspace's testing conventions (this project's own live network dependency
is inherent to its purpose, but automated tests must never rely on it)."""

import os
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.testing import make_ohlcv_from_closes
from fundamental_screener.fscreen.config import ScreenerConfig
from fundamental_screener.fscreen.strategy import FundamentalMarginOfSafetyStrategy


def _linear_universe(n=400, drifts=None):
    drifts = drifts or {}
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    universe = {}
    for sym, drift in drifts.items():
        df = make_ohlcv_from_closes(100.0 + drift * t)
        df.index = dates
        universe[sym] = df
    return universe


def _fake_metadata(table):
    def _fetch(symbol, provider=None, **kwargs):
        return table.get(symbol, {
            "roe": float("nan"), "dividend_yield": float("nan"),
            "earnings_growth": float("nan"), "debt_to_equity": float("nan"),
        })
    return _fetch


GOOD_META = {"roe": 0.35, "dividend_yield": 0.03, "earnings_growth": 0.10, "debt_to_equity": 50.0}
BAD_META = {"roe": 0.05, "dividend_yield": 0.01, "earnings_growth": 0.01, "debt_to_equity": 200.0}


def test_generate_weights_honors_params_overrides_not_just_config_defaults():
    # Regression: a backtester-reconstructed instance is always
    # FundamentalMarginOfSafetyStrategy() (zero-arg, default ScreenerConfig)
    # -- generate_weights must still honor screener-tuned thresholds saved
    # in strategy.json's `params`, not silently fall back to class defaults.
    universe = _linear_universe(drifts={"PG": 0.20, "SPY": 0.05, "BIL": 0.0})
    strat = FundamentalMarginOfSafetyStrategy()  # default min_roe=0.15 -- PG's 0.05 ROE fails this

    with patch("fundamental_screener.fscreen.fundamentals.fetch_fund_metadata", side_effect=_fake_metadata({"PG": BAD_META})):
        default_weights = strat.generate_weights(universe, {"universe": ["PG"], "benchmark_symbol": "SPY", "lookback_days": 100})
        assert (default_weights.ffill().fillna(0.0)["PG"] == 0).all(), "PG must fail the default min_roe=0.15 gate"

        # Relaxing min_roe via params (as a screener export with --min-roe
        # 0.01 would) must actually take effect, not be discarded.
        relaxed_weights = strat.generate_weights(universe, {
            "universe": ["PG"], "benchmark_symbol": "SPY", "lookback_days": 100,
            "min_roe": 0.01, "required_return": 0.01, "max_debt_to_equity": 500.0,
            "min_earnings_growth": 0.0,
        })
    daily = relaxed_weights.ffill().fillna(0.0)
    assert (daily["PG"] > 0).any(), "relaxed min_roe/required_return via params must actually be honored"


def test_generate_weights_caches_fundamentals_across_calls_on_same_instance():
    # Regression: backtester.run_walkforward calls generate_weights() once
    # per fold on the SAME template instance -- fundamentals are meant to
    # be one constant per-run snapshot, not re-fetched (and billed/rate-
    # limited) once per fold.
    universe = _linear_universe(drifts={"KO": 0.20, "SPY": 0.05, "BIL": 0.0})
    cfg = ScreenerConfig(universe=["KO"], benchmark_symbol="SPY", lookback_days=100, required_return=0.10)
    strat = FundamentalMarginOfSafetyStrategy(cfg)

    with patch(
        "fundamental_screener.fscreen.fundamentals.fetch_fund_metadata", side_effect=_fake_metadata({"KO": GOOD_META}),
    ) as mock_fetch:
        strat.generate_weights(universe)
        strat.generate_weights(universe)
        strat.generate_weights(universe)

    assert mock_fetch.call_count == 1, "fundamentals must be fetched once and reused across repeated calls, not once per call"


def test_strategy_holds_quality_symbol_that_clears_the_hurdle():
    universe = _linear_universe(drifts={"KO": 0.20, "SPY": 0.05, "BIL": 0.0})
    cfg = ScreenerConfig(universe=["KO"], benchmark_symbol="SPY", lookback_days=100, required_return=0.10)
    strat = FundamentalMarginOfSafetyStrategy(cfg)

    with patch("fundamental_screener.fscreen.fundamentals.fetch_fund_metadata", side_effect=_fake_metadata({"KO": GOOD_META})):
        weights = strat.generate_weights(universe)

    assert not weights.empty
    daily = weights.ffill().fillna(0.0)
    assert (daily["KO"] > 0).any()


def test_strategy_never_holds_symbol_failing_the_quality_gate():
    universe = _linear_universe(drifts={"PG": 0.20, "SPY": 0.05, "BIL": 0.0})
    cfg = ScreenerConfig(universe=["PG"], benchmark_symbol="SPY", lookback_days=100, required_return=0.10)
    strat = FundamentalMarginOfSafetyStrategy(cfg)

    with patch("fundamental_screener.fscreen.fundamentals.fetch_fund_metadata", side_effect=_fake_metadata({"PG": BAD_META})):
        weights = strat.generate_weights(universe)

    daily = weights.ffill().fillna(0.0)
    assert (daily["PG"] == 0).all(), "low ROE / high leverage must exclude the symbol regardless of its price trend"


def test_strategy_exits_once_benchmark_trailing_return_catches_up():
    n = 400
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    # KO's own expected_return (from GOOD_META: 0.10 + 0.03 = 0.13) is
    # constant; SPY's own trailing return is engineered to start LOW (so KO
    # enters) then ramp up past 0.13 (so KO must exit).
    spy_close = np.concatenate([100.0 + 0.01 * t[:250], 100.0 + 0.01 * 249 + 0.35 * (t[250:] - 249)])
    universe = {
        "KO": make_ohlcv_from_closes(100.0 + 0.20 * t),
        "SPY": make_ohlcv_from_closes(spy_close),
        "BIL": make_ohlcv_from_closes([100.0] * n),
    }
    for df in universe.values():
        df.index = dates

    cfg = ScreenerConfig(universe=["KO"], benchmark_symbol="SPY", lookback_days=100, required_return=0.05)
    strat = FundamentalMarginOfSafetyStrategy(cfg)
    with patch("fundamental_screener.fscreen.fundamentals.fetch_fund_metadata", side_effect=_fake_metadata({"KO": GOOD_META})):
        weights = strat.generate_weights(universe)

    daily = weights.ffill().fillna(0.0)
    assert (daily["KO"].iloc[100:250] > 0).any(), "should be held once warmed up and before SPY's return ramps up"
    assert (daily["KO"].iloc[-30:] == 0).all(), "should exit once SPY's trailing return catches up to KO's constant expected return"


def test_strategy_returns_empty_when_no_candidates_in_universe():
    universe = _linear_universe(drifts={"SPY": 0.05, "BIL": 0.0})
    strat = FundamentalMarginOfSafetyStrategy(ScreenerConfig(universe=["KO", "PG"]))
    assert strat.generate_weights(universe).empty


def test_warmup_bars_equals_lookback_days():
    strat = FundamentalMarginOfSafetyStrategy(ScreenerConfig(lookback_days=756))
    assert strat.warmup_bars() == 756


def test_explain_weights_mentions_the_constant_signal_limitation():
    strat = FundamentalMarginOfSafetyStrategy()
    explanation = strat.explain_weights()
    assert "CONSTANT" in explanation
    assert "docs/snowball_strategy.txt" in explanation

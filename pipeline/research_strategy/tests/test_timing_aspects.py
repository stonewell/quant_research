"""Unit tests for research_strategy/rs/timing_aspects.py (Track B: single-
asset timing entry x exit/risk composition). Guaranteed 100% offline using
synthetic OHLCV data generators from common/testing.py."""

import os
import sys
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.testing import make_ohlcv_from_closes, make_oscillating_df, make_trending_pullback_df
from research_strategy.rs.config import StrategyConfig
from research_strategy.rs.strategy import (
    ChanPivotShiftStrategy,
    RSIMeanReversionStrategy,
    SwingTrendPullbackStrategy,
    TurtleBreakoutStrategy,
)
from research_strategy.rs.timing_aspects import (
    ENTRY_SIGNAL_ASPECTS,
    EXIT_RISK_ASPECTS,
    TIMING_TEMPLATE_ASPECTS,
    CompositeTimingTemplate,
    build_composite_timing_candidates,
)
from research_strategy.tests.test_strategy import _chan_breakout_closes, _timing_universe, _daily


PARITY_CASES = [
    (RSIMeanReversionStrategy, lambda: _timing_universe(make_oscillating_df(n=500, seed=7))),
    (SwingTrendPullbackStrategy, lambda: _timing_universe(make_trending_pullback_df(n=500, seed=7))),
    (ChanPivotShiftStrategy, lambda: _timing_universe(make_ohlcv_from_closes(_chan_breakout_closes()))),
    (TurtleBreakoutStrategy, lambda: _timing_universe(make_ohlcv_from_closes(_turtle_breakout_closes()))),
]


def _turtle_breakout_closes():
    n = 250
    closes = np.linspace(100.0, 120.0, n)
    closes[150] = 135.0
    closes[151:] = 135.0
    return closes


@pytest.mark.parametrize("atomic_cls,make_universe", PARITY_CASES)
def test_composite_reproduces_atomic_template_exactly(atomic_cls, make_universe):
    """Pairing a decomposable timing template's own (entry, exit) key pair
    back together via CompositeTimingTemplate, given that SAME template's
    own resolved StrategyConfig as params, must reproduce identical weights
    to the original class."""
    universe = make_universe()
    cfg = StrategyConfig(turtle_require_trend_filter=False) if atomic_cls is TurtleBreakoutStrategy else StrategyConfig()
    atomic = atomic_cls(cfg)

    entry_key, exit_key = TIMING_TEMPLATE_ASPECTS[atomic_cls.__name__]
    composite = CompositeTimingTemplate(ENTRY_SIGNAL_ASPECTS[entry_key], EXIT_RISK_ASPECTS[exit_key])
    params = asdict(cfg)

    expected = atomic.generate_weights(universe, None)
    actual = composite.generate_weights(universe, params)

    pd.testing.assert_frame_equal(actual, expected, check_dtype=False)


def test_composite_timing_mixes_entry_from_one_template_with_exit_from_another():
    # Turtle's breakout entry, paired with RSI's simpler stop-loss/max-holding-days exit.
    universe = _timing_universe(make_ohlcv_from_closes(_turtle_breakout_closes()))
    cfg = StrategyConfig(turtle_require_trend_filter=False)
    composite = CompositeTimingTemplate(
        ENTRY_SIGNAL_ASPECTS["turtle_breakout_entry"], EXIT_RISK_ASPECTS["rsi_cross_exit"],
    )
    weights = composite.generate_weights(universe, asdict(cfg))
    daily = _daily(weights)

    # Entered on the breakout bar (same entry logic as TurtleBreakoutStrategy).
    assert daily["SPY"].iloc[150] > 0.0
    # This is NOT TurtleBreakoutStrategy's own weights (different exit rule).
    atomic = TurtleBreakoutStrategy(cfg)
    atomic_daily = _daily(atomic.generate_weights(universe, None))
    assert not daily["SPY"].equals(atomic_daily["SPY"])


def test_composite_timing_only_trades_entry_aspects_own_symbol_not_full_risky_universe():
    """Regression: `p` (the merged StrategyConfig) used to be passed as
    _get_risky_symbols' own "explicit override" argument -- but
    StrategyConfig.risky_universe defaults to a non-empty 8-symbol list on
    EVERY instance, so it always outranked cfg_symbol, silently trading the
    whole default risky_universe instead of just the entry aspect's own
    configured single symbol (e.g. "SPY" for rsi_oversold_entry)."""
    n = 200
    flat = make_ohlcv_from_closes(np.full(n, 100.0), start="2020-01-01")
    # Oscillating: would reliably trigger RSI(2) oversold entries if any of
    # these were actually included as a risky symbol.
    oscillating = make_oscillating_df(n=n, seed=3)

    universe = {"SPY": flat, "BIL": flat.copy()}
    for sym in ("QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ"):
        universe[sym] = oscillating.copy()

    cfg = StrategyConfig()  # rsi_symbol="SPY"; risky_universe=DEFAULT_RISKY_UNIVERSE (8 symbols)
    composite = CompositeTimingTemplate(ENTRY_SIGNAL_ASPECTS["rsi_oversold_entry"], EXIT_RISK_ASPECTS["rsi_cross_exit"])
    weights = composite.generate_weights(universe, asdict(cfg))
    dense = weights.ffill().fillna(0.0)

    # SPY is flat -- RSI never dips oversold, so nothing should ever enter.
    # If the bug were still present, the oscillating QQQ/IWM/... symbols
    # (wrongly included via risky_universe) would show real nonzero entries.
    for sym in ("QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ"):
        assert (dense[sym] == 0.0).all(), f"{sym} should never be traded -- only SPY is the configured symbol"


def test_build_composite_timing_candidates_needs_at_least_two_decomposable_templates():
    fake_best = {
        "rsi_mean_reversion": {"template": RSIMeanReversionStrategy(), "score": 1.0, "res": {}, "params": {}},
    }
    assert build_composite_timing_candidates(fake_best, top_k=4) == []


def test_build_composite_timing_candidates_cross_breeds_and_skips_existing_pairs():
    fake_best = {
        "rsi_mean_reversion": {
            "template": RSIMeanReversionStrategy(StrategyConfig(rsi_period=3)), "score": 1.0, "res": {}, "params": {},
        },
        "swing_trend_pullback": {
            "template": SwingTrendPullbackStrategy(StrategyConfig()), "score": 0.9, "res": {}, "params": {},
        },
    }
    candidates = build_composite_timing_candidates(fake_best, top_k=4)
    names = {t.name for t, _ in candidates}

    assert names == {"rsi_oversold_entry__swing_stop_target_exit", "swing_pullback_entry__rsi_cross_exit"}

    for template, params in candidates:
        if template.name == "rsi_oversold_entry__swing_stop_target_exit":
            assert params["rsi_period"] == 3  # entry side's own config wins for overlapping-purpose fields

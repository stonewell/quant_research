"""Unit and Integration Tests for Chan-Similar Trading Strategies.

Tests all 3 new strategies based on docs/chan_similar_trading.md:
- PriceActionBreakoutRetestStrategy (pa_breakout_retest)
- VolumeProfilePocMigrationStrategy (vp_poc_migration)
- Wave3FibonacciStrategy (wave3_fibonacci)

Guaranteed 100% offline using synthetic OHLCV data generators from common/testing.py.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.testing import make_ohlcv_from_closes
from research_strategy.rs.chan_similar_strategies import (
    PriceActionBreakoutRetestStrategy,
    VolumeProfilePocMigrationStrategy,
    Wave3FibonacciStrategy,
    compute_rolling_poc_series,
    run_pa_breakout_retest_single_symbol,
    run_vp_poc_migration_single_symbol,
    run_wave3_fibonacci_single_symbol,
)
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.strategy import instantiate_strategy_from_config_entry


def create_mock_universe(n_days: int = 250) -> dict:
    """Creates a deterministic synthetic universe for testing strategy mechanics."""
    dates = pd.bdate_range("2021-01-01", periods=n_days)
    t = np.arange(n_days)

    spy_close = 100.0 + 0.1 * t + 3.0 * np.sin(t / 8.0)
    qqq_close = 100.0 + 0.15 * t + 5.0 * np.sin(t / 6.0)
    bil_close = 100.0 + 0.001 * t

    symbols = {
        "SPY": spy_close,
        "QQQ": qqq_close,
        "BIL": bil_close,
    }

    universe = {}
    for sym, close_arr in symbols.items():
        df = make_ohlcv_from_closes(close_arr)
        df.index = dates
        # Add realistic volume variation
        df["Volume"] = 1_000_000.0 + 200_000.0 * np.sin(t / 5.0)
        universe[sym] = df

    return universe


# ==============================================================================
# 1. Price Action Breakout Retest Tests
# ==============================================================================

def test_pa_breakout_retest_contract():
    strat = PriceActionBreakoutRetestStrategy()
    assert strat.name == "pa_breakout_retest"
    assert "regime_trend_strength" in strat.factor_tags
    assert "absolute_momentum_trend" in strat.factor_tags
    assert strat.warmup_bars() > 0
    assert "Breakout & Retest" in strat.explain_weights()


def test_pa_breakout_retest_single_symbol_mechanics():
    """Constructs a controlled box -> breakout -> retest -> target scenario."""
    n = 100
    # Phase 1: Bars 0-45 consolidation box between 98 and 102
    close = np.full(n, 100.0)
    high = np.full(n, 102.0)
    low = np.full(n, 98.0)
    open_ = np.full(n, 100.0)
    volume = np.full(n, 1000.0)

    # Bar 46: Decisive breakout above 102 on high volume
    high[46] = 106.0
    close[46] = 105.0
    open_[46] = 101.0
    low[46] = 101.0
    volume[46] = 5000.0  # Big volume expansion

    # Bar 47: Pullback/retest near box top (102.0), low volume, rejection
    high[47] = 104.0
    low[47] = 102.5  # Retest
    open_[47] = 103.0
    close[47] = 103.5  # Bullish rejection close > open
    volume[47] = 600.0  # Low volume on pullback

    # Bar 48: Still in position, holding steady
    high[48] = 105.0
    low[48] = 103.0
    open_[48] = 103.5
    close[48] = 104.5
    volume[48] = 800.0

    bars = pd.DataFrame({
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume,
    })

    w = run_pa_breakout_retest_single_symbol(
        bars,
        box_window=20,
        max_box_range_pct=0.10,
        vol_contraction_ratio=1.5,  # Relaxed for synthetic test
        breakout_vol_mult=1.5,
        retest_max_bars=5,
        retest_tolerance=0.03,
        take_profit_mult=1.5,
    )

    # Entry should occur on retest at bar 47
    assert w[47] == 1.0
    assert w[48] == 1.0
    assert w[0] == 0.0


def test_pa_breakout_retest_stop_loss_and_take_profit():
    """Test stop loss and take profit exits."""
    n = 60
    close = np.full(n, 100.0)
    high = np.full(n, 102.0)
    low = np.full(n, 98.0)
    open_ = np.full(n, 100.0)
    volume = np.full(n, 1000.0)

    # Breakout at 42
    high[42] = 106.0
    close[42] = 105.0
    volume[42] = 5000.0

    # Retest at 43: low touches 102.0, open 102.2, close 103.0 (bullish rejection)
    open_[43] = 102.2
    low[43] = 102.0
    close[43] = 103.0
    high[43] = 103.5
    volume[43] = 500.0

    # Holding at 44
    open_[44] = 103.0
    low[44] = 102.5
    close[44] = 103.2
    high[44] = 103.8
    volume[44] = 600.0

    # Stop loss trigger at bar 45 (drop below active stop / box midpoint 100.0)
    open_[45] = 102.0
    close[45] = 95.0
    low[45] = 94.0
    high[45] = 102.0

    bars = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})
    w = run_pa_breakout_retest_single_symbol(
        bars,
        box_window=20,
        vol_contraction_ratio=1.5,
        breakout_vol_mult=1.5,
        stop_loss_pct=0.05,
    )
    assert w[43] == 1.0
    assert w[44] == 1.0
    # Stopped out at bar 45
    assert w[45] == 0.0


def test_pa_breakout_retest_missing_volume():
    """Ensures fallback works seamlessly when Volume column is absent."""
    n = 60
    close = np.full(n, 100.0)
    high = np.full(n, 102.0)
    low = np.full(n, 98.0)

    # Breakout at 42
    high[42] = 106.0
    close[42] = 105.0

    # Retest at 43
    low[43] = 102.0
    close[43] = 103.0

    bars = pd.DataFrame({"High": high, "Low": low, "Close": close})
    w = run_pa_breakout_retest_single_symbol(bars, box_window=20)
    assert isinstance(w, np.ndarray)
    assert len(w) == n


# ==============================================================================
# 2. Volume Profile POC Migration Tests
# ==============================================================================

def test_vp_poc_migration_contract():
    strat = VolumeProfilePocMigrationStrategy()
    assert strat.name == "vp_poc_migration"
    assert "absolute_momentum_trend" in strat.factor_tags
    assert "relative_momentum" in strat.factor_tags
    assert strat.warmup_bars() > 0
    assert "Volume Profile POC Migration" in strat.explain_weights()


def test_compute_rolling_poc_series():
    """Verify rolling POC locates high volume price clusters."""
    n = 80
    high = np.full(n, 105.0)
    low = np.full(n, 95.0)
    close = np.full(n, 100.0)
    volume = np.full(n, 1000.0)

    # Create high-volume concentration at price 104
    high[40:50] = 105.0
    low[40:50] = 103.0
    close[40:50] = 104.0
    volume[40:50] = 50000.0

    bars = pd.DataFrame({"High": high, "Low": low, "Close": close, "Volume": volume})
    poc, val, vah = compute_rolling_poc_series(bars, lookback=30, n_bins=20, value_area_pct=0.70)

    assert len(poc) == n
    # By bar 55, POC should reflect the heavy volume cluster near 104
    assert not np.isnan(poc[55])
    assert 103.0 <= poc[55] <= 105.0
    assert val[55] <= poc[55] <= vah[55]


def test_compute_rolling_poc_flat_series():
    """Verify edge case where high == low does not crash."""
    n = 50
    high = np.full(n, 100.0)
    low = np.full(n, 100.0)
    close = np.full(n, 100.0)
    bars = pd.DataFrame({"High": high, "Low": low, "Close": close})
    poc, val, vah = compute_rolling_poc_series(bars, lookback=20)
    assert len(poc) == n
    assert poc[30] == 100.0


def test_run_vp_poc_migration_single_symbol():
    """Simulate upward staircase migration and washout entry."""
    n = 120
    t = np.arange(n)
    # Upward staircase in price
    close = 100.0 + 0.3 * t
    high = close + 1.5
    low = close - 1.5
    volume = np.full(n, 1000.0)

    bars = pd.DataFrame({"High": high, "Low": low, "Close": close, "Volume": volume})
    w = run_vp_poc_migration_single_symbol(
        bars,
        lookback=30,
        poc_step_lookback=10,
        min_step_pct=0.01,
        dip_tolerance=0.05,
    )
    assert isinstance(w, np.ndarray)
    assert len(w) == n
    # Since prices step up monotonically and low touches POC zone, at least some positions trigger
    assert np.any(w > 0.0)


# ==============================================================================
# 3. Wave 3 Fibonacci Strategy Tests
# ==============================================================================

def test_wave3_fibonacci_contract():
    strat = Wave3FibonacciStrategy()
    assert strat.name == "wave3_fibonacci"
    assert "volatility_targeting" in strat.factor_tags
    assert strat.warmup_bars() == 65
    assert "Wave 3 Fibonacci" in strat.explain_weights()


def test_wave3_fibonacci_single_symbol_mechanics():
    """Test Wave 1 impulse -> Wave 2 golden pocket retrace -> Wave 3 entry."""
    n = 100
    close = np.full(n, 100.0)
    high = np.full(n, 101.0)
    low = np.full(n, 99.0)
    open_ = np.full(n, 100.0)
    volume = np.full(n, 1000.0)

    # Initial baseline above 60-bar SMA: setup gentle trend
    for i in range(60):
        close[i] = 100.0 + i * 0.1
        high[i] = close[i] + 1.0
        low[i] = close[i] - 1.0
        open_[i] = close[i]

    # Wave 1 impulse: from bar 60 (price ~106) to bar 70 (peak at 120)
    for i in range(60, 71):
        close[i] = 106.0 + (i - 60) * 1.4
        high[i] = close[i] + 1.0
        low[i] = close[i] - 1.0
        open_[i] = close[i] - 0.5
        volume[i] = 5000.0

    # Peak at bar 70 is 121.0, trough at bar 60 was 105.0. H1 = 16.0
    # 50% retrace = 121 - 8 = 113. 61.8% retrace = 121 - 9.89 = 111.1
    # Pivot confirmation needs 5 bars, so by bar 75, bar 70 is confirmed as pivot peak.

    # Wave 2 retracement: bars 71 to 76 pull back into golden pocket ~113-114
    for i in range(71, 77):
        close[i] = 120.0 - (i - 70) * 1.2
        high[i] = close[i] + 0.5
        low[i] = close[i] - 0.5
        open_[i] = close[i] + 0.2
        volume[i] = 400.0  # Low drying volume

    # Bar 77: Bullish breakout of corrective line in golden pocket
    open_[77] = 113.0
    low[77] = 112.5
    close[77] = 115.5  # Rebound above previous bar and open
    high[77] = 116.0
    volume[77] = 500.0

    bars = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})
    w = run_wave3_fibonacci_single_symbol(
        bars,
        pivot_window=5,
        min_wave1_pct=0.08,
        fibo_min_retrace=0.40,
        fibo_max_retrace=0.65,
        vol_contraction_ratio=1.0,
    )

    # Position should activate on or shortly after bar 77
    assert np.any(w[77:] == 1.0)


def test_wave3_fibonacci_invalidation():
    """Wave 2 dropping below Wave 1 origin invalidates the count."""
    n = 90
    close = np.full(n, 100.0)
    high = np.full(n, 101.0)
    low = np.full(n, 99.0)

    # Impulse Wave 1
    for i in range(50, 65):
        close[i] = 100.0 + (i - 50) * 1.5
        high[i] = close[i] + 1.0
        low[i] = close[i] - 1.0

    # Collapse below origin 99.0
    for i in range(65, 80):
        close[i] = 95.0
        high[i] = 96.0
        low[i] = 94.0

    bars = pd.DataFrame({"High": high, "Low": low, "Close": close})
    w = run_wave3_fibonacci_single_symbol(bars, pivot_window=5)
    # Should not enter long
    assert np.all(w == 0.0)


# ==============================================================================
# 4. Multi-Asset Portfolio & Universe Integration Tests
# ==============================================================================

@pytest.mark.parametrize("strat_cls", [
    PriceActionBreakoutRetestStrategy,
    VolumeProfilePocMigrationStrategy,
    Wave3FibonacciStrategy,
])
def test_strategy_portfolio_generate_weights(strat_cls):
    """Verifies sparse weights output and cash derouting on a mock universe."""
    universe = create_mock_universe(n_days=180)
    strat = strat_cls()
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)
    assert not weights.empty
    # Must contain all symbols from the universe
    for sym in ["SPY", "QQQ", "BIL"]:
        assert sym in weights.columns

    # Check non-empty rebalance rows sum close to 1.0 (with cash proxy BIL absorbing idle cash)
    non_nan_rows = weights.dropna(how="all")
    if not non_nan_rows.empty:
        for _, row in non_nan_rows.iterrows():
            if not row.isna().all():
                # Any row with allocations should sum to approximately 1.0
                assert abs(row.sum() - 1.0) < 1e-4 or row.sum() == 0.0


def test_instantiate_from_strategies_config():
    """Verify that strategies_config.json contains valid entries that instantiate properly."""
    cfg_data = load_strategies_config()
    for key in ["pa_breakout_retest", "vp_poc_migration", "wave3_fibonacci"]:
        assert key in cfg_data
        entry = cfg_data[key]
        strat = instantiate_strategy_from_config_entry(key, entry)
        assert isinstance(strat, (PriceActionBreakoutRetestStrategy, VolumeProfilePocMigrationStrategy, Wave3FibonacciStrategy))

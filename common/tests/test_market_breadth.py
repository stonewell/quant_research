"""Unit tests for the L2.63 market breadth / thrust helpers.

`common.position_policy`'s gate 5 (「宏观冲力爆量突破」) needs a per-bar
`market_breadth`, and before L2.63 NO backtest caller ever supplied one:
`simulate_position_lifecycle(market_breadths=None)` was the only value
`backtester/run_backtest.py` (standard + walkforward) and
`common/allocation_backtester.py` ever passed, so `is_breadth` was permanently
False and the confluence leg was dead code outside live trading.

These tests pin the SHARED derivation (`common.indicators`), 100% offline:
  * the share-above-SMA formula against a HAND-COMPUTED expectation;
  * the thrust-selection rule (`thrust if thrust >= threshold else breadth`)
    against the live runner's `market_breadth=thrust_val if thrust_active
    else breadth_val`;
  * the alignment rule (union of the symbols' own bar stamps, no price ffill);
  * the NaN-not-zero contract (warmup bars are NaN -> the kernel's `None`,
    never a fabricated 0.0 that would pin `is_breadth` to False forever);
  * the one-time WARNING when the universe carries no `Volume` column (the
    gate's volume legs cannot be evaluated then, and silence was the bug).
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common import position_lifecycle as _pl
from common.indicators import (
    lifecycle_market_breadth,
    market_breadth_series,
    market_thrust_series,
)


def _frame(values, start="2020-01-01"):
    """A price-only frame whose ONLY column is `Close` (breadth needs nothing else)."""
    return pd.DataFrame(
        {"Close": np.asarray(values, dtype=float)},
        index=pd.bdate_range(start, periods=len(values)),
    )


def test_market_breadth_series_matches_manual_computation():
    """Five synthetic symbols; the share-above-SMA50 per bar is hand-computed.

    Per symbol closes (60 bars, SMA window = full 50):
      A: 100..159 strictly rising            -> above                 (1)
      B: 200..141 strictly falling           -> below                 (0)
      C: constant 50                          -> close == SMA, not >  (0)
      D: 100..159 rising (different slope)   -> above                 (1)
      E: 100 for bars 0..48, 150 for 49..52, then 20 for 53..59.
         Bars 49..52: close 150 vs means 101.0/102.0/103.0/104.0 -> above (1)
         Bar 53: window 4..53 = 45x100 + 4x150 + 20 = 5120 -> mean 102.4,
                  close 20 -> below (0)
         Bar 54: window 5..54 = 44x100 + 4x150 + 2x20 = 5040 -> mean 100.8,
                  close 20 -> below (0)  (bars 55..59 likewise below)

    So the share is NaN for bars 0..48 (nobody has 50 observations yet),
    3/5 = 0.6 for bars 49..52, and 2/5 = 0.4 for bars 53..59.
    """
    n = 60
    e = np.concatenate([np.full(49, 100.0), np.full(4, 150.0), np.full(7, 20.0)])
    assert len(e) == n
    universe = {
        "A": _frame(np.linspace(100.0, 159.0, n)),
        "B": _frame(np.linspace(200.0, 141.0, n)),
        "C": _frame(np.full(n, 50.0)),
        "D": _frame(np.linspace(100.0, 159.0, n) * 1.5),
        "E": _frame(e),
    }

    breadth = market_breadth_series(universe, lookback=50)

    expected = np.concatenate([np.full(49, np.nan), np.full(4, 0.6), np.full(7, 0.4)])
    assert len(breadth) == n
    np.testing.assert_allclose(breadth.to_numpy(dtype=float), expected, equal_nan=True)


def test_market_breadth_series_index_is_the_union_and_never_fills_prices():
    """Alignment rule: union of the symbols' own stamps, no forward-filled close.

    Symbol `LATE` only lists on the last three bars. If the union were a
    forward-filled price panel, those three bars would silently gain a "flat"
    observation for `EARLY` (creating fake SMA inputs); with the documented
    rule, `EARLY` simply has no bar there and drops out of that bar's
    denominator, and `LATE` (insufficient history) contributes nothing.
    """
    idx = pd.bdate_range("2021-01-04", periods=6)
    early = pd.DataFrame({"Close": np.linspace(10.0, 15.0, 6)}, index=idx)
    late_idx = idx[-3:]
    late = pd.DataFrame({"Close": [100.0, 101.0, 102.0]}, index=late_idx)

    breadth = market_breadth_series({"EARLY": early, "LATE": late}, lookback=5)

    assert list(breadth.index) == list(idx)          # union, not intersection
    assert breadth.notna().sum() == 2                # EARLY has 5 obs at bars 4,5
    # Bars 0..3: nobody has lookback=5 observations -> NaN, not 0.0.
    assert breadth.iloc[:4].isna().all()
    assert (breadth == 0.0).sum() == 0
    # At bars 4/5 EARLY is the only measurable symbol and is rising -> 1.0.
    assert breadth.iloc[4] == pytest.approx(1.0)
    assert breadth.iloc[5] == pytest.approx(1.0)


def test_market_thrust_series_matches_manual_computation():
    """`thrust` = share with a POSITIVE `lookback`-bar return."""
    n = 8
    universe = {
        # rising: roc > 0 on every bar after the shift
        "A": _frame([10, 11, 12, 13, 14, 15, 16, 17]),
        # falling: roc < 0
        "B": _frame([20, 19, 18, 17, 16, 15, 14, 13]),
        # exactly flat: roc == 0 -> NOT positive (strict comparison, live parity)
        "C": _frame([5, 5, 5, 5, 5, 5, 5, 5]),
    }
    thrust = market_thrust_series(universe, lookback=2)

    n_rows = n - 2
    assert thrust.iloc[:2].isna().all()
    np.testing.assert_allclose(
        thrust.iloc[2:].to_numpy(dtype=float), np.full(n_rows, 1.0 / 3.0)
    )


def test_lifecycle_market_breadth_missing_history_is_nan_not_zero():
    """Warmup bars must be NaN (-> kernel `None`), and 0.0 is never fabricated.

    A filled 0.0 would reproduce the exact dead-gate bug L2.63 fixes: `0.0 >=
    0.60` is False on EVERY bar, permanently and silently. So the assertion is
    strict: no NaN is replaced with a number, and no usable value is 0.0.
    """
    universe = {"A": _frame(np.linspace(100.0, 120.0, 60))}

    # The breadth statistic itself: 50 full observations required, so bars 0..48
    # are NaN, not a fabricated 0.0 (which would pin `is_breadth` to False).
    breadth = market_breadth_series(universe, 50)
    assert breadth.iloc[:49].isna().all(), "insufficient history must stay NaN"
    assert breadth.iloc[49:].notna().all()
    assert (breadth.dropna() == 0.0).sum() == 0

    # The combined gate-5 value: the THRUST leg only needs `thrust_lookback`
    # bars, so it becomes measurable earlier than breadth -- the first 10 bars
    # are still NaN because neither leg has enough history.
    series = lifecycle_market_breadth(universe, breadth_lookback=50, thrust_lookback=10)
    assert series.iloc[:10].isna().all(), "insufficient history must stay NaN"
    assert series.iloc[10:].notna().all()
    assert (series.dropna() == 0.0).sum() == 0

    # A universe with NO usable history at all is entirely NaN -- never 0.0 and
    # never a 0.50 placeholder (the live Stage-1 placeholder is a scalar
    # regime read, not a measurable breadth).
    tiny = {"A": _frame([100.0, 101.0, 102.0])}
    empty_history = lifecycle_market_breadth(
        tiny, breadth_lookback=50, thrust_lookback=10
    )
    assert empty_history.isna().all()
    assert (empty_history == 0.0).sum() == 0


def test_lifecycle_market_breadth_is_thrust_when_thrust_active_else_breadth():
    """Live parity: `market_breadth = thrust if thrust >= thresh else breadth`.

    Constructed so the two statistics DISAGREE, otherwise the rule is
    unfalsifiable: breadth is pinned at 1.0 (every close above its SMA) while
    the last bar's thrust drops to 0.0 (a one-bar crash). With
    `thrust_thresh=0.60` the value must therefore fall back to BREADTH, not to
    the thrust reading.
    """
    n = 60
    # 4 symbols strictly rising for the whole series -> breadth 1.0.
    rising = np.linspace(100.0, 159.0, n)
    universe = {f"S{i}": _frame(rising) for i in range(4)}

    series = lifecycle_market_breadth(
        universe, breadth_lookback=50, thrust_lookback=10, thrust_thresh=0.60
    )
    assert series.iloc[-1] == pytest.approx(1.0)
    assert market_thrust_series(universe, 10).iloc[-1] == pytest.approx(1.0)

    # Now dip the last bar of every symbol below its 10-bar-ago close (thrust ->
    # 0.0) while it stays ABOVE its 50-bar mean (breadth stays 1.0), which is the
    # only shape that can distinguish "fell back to breadth" from "used thrust".
    # close[i] == 100 + i, so close[-11] == 149 and the trailing mean is ~134.
    crashed = {sym: df.copy() for sym, df in universe.items()}
    for df in crashed.values():
        df.iloc[-1, df.columns.get_loc("Close")] = 148.0
    thrust_last = market_thrust_series(crashed, 10).iloc[-1]
    breadth_last = market_breadth_series(crashed, 50).iloc[-1]
    assert thrust_last == pytest.approx(0.0)
    assert breadth_last == pytest.approx(1.0)

    mixed = lifecycle_market_breadth(
        crashed, breadth_lookback=50, thrust_lookback=10, thrust_thresh=0.60
    )
    assert mixed.iloc[-1] == pytest.approx(1.0), "inactive thrust must fall back to breadth"

    # And when thrust DOES clear the threshold it WINS over breadth.
    always_thrusty = lifecycle_market_breadth(
        universe, breadth_lookback=50, thrust_lookback=10, thrust_thresh=0.0
    )
    assert always_thrusty.iloc[-1] == pytest.approx(1.0)


def test_lifecycle_market_breadth_excludes_the_cash_proxy():
    """The idle-cash sleeve is not a breadth observation (live Stage-1 parity)."""
    n = 60
    rising = np.linspace(100.0, 159.0, n)
    universe = {"A": _frame(rising), "B": _frame(rising)}

    # A cash proxy whose price never moves is BELOW/at its own SMA (constant ==
    # mean -> not "above"), so counting it would drag breadth to 2/3.
    cash = pd.DataFrame({"Close": np.full(n, 100.0)}, index=pd.bdate_range("2020-01-01", periods=n))
    with_cash = dict(universe, BIL=cash)

    assert market_breadth_series(with_cash, 50, cash_proxy="BIL").iloc[-1] == pytest.approx(1.0)
    assert market_breadth_series(with_cash, 50).iloc[-1] == pytest.approx(2.0 / 3.0)


def test_missing_volume_warns_once_and_names_the_gate(monkeypatch):
    """No `Volume` column => gate 5's volume legs are inert: SAY SO, once."""
    monkeypatch.setattr(_pl, "_VOLUME_WARNED_CONTEXTS", set())
    price_only = {"A": _frame(np.linspace(100.0, 120.0, 60))}

    assert _pl.lifecycle_volume_available(price_only) is False
    with pytest.warns(RuntimeWarning, match="no universe frame carries a `Volume` column"):
        assert _pl.warn_if_lifecycle_volume_missing(price_only, context="unit-test") is False

    # Second call for the same context is silent (one warning per run/fold).
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        _pl.warn_if_lifecycle_volume_missing(price_only, context="unit-test")

    # A frame WITH Volume is available and silent.
    with_volume = {"A": price_only["A"].assign(Volume=1000.0)}
    assert _pl.lifecycle_volume_available(with_volume) is True
    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        assert _pl.warn_if_lifecycle_volume_missing(with_volume, context="unit-test-2") is True

"""Unit tests for amine/strategy.py AlphaMiningStrategy.

100% offline: uses synthetic universes and precomputed score panels only.
Verifies sparse weights contract, explicit 0.0 for excluded symbols,
semantic version == "1.1.0", and ZERO lightgbm import dependency.
"""

import builtins
import importlib
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_AM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AM_ROOT not in sys.path:
    sys.path.insert(0, _AM_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd

from common.testing import make_ohlcv_from_closes


def _make_universe(n_dates: int = 25, symbols=("A", "B", "C", "D")):
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    universe = {}
    for i, sym in enumerate(symbols):
        closes = 100.0 + (i + 1) * np.linspace(0.0, 10.0, n_dates)
        df = make_ohlcv_from_closes(closes)
        df.index = dates
        universe[sym] = df
    return universe, dates


def test_strategy_imports_without_lightgbm(monkeypatch):
    """Verify `import amine.strategy` never touches or requires `lightgbm`."""
    saved_modules = {}
    for mod_name in list(sys.modules.keys()):
        if mod_name == "lightgbm" or mod_name.startswith("lightgbm.") or mod_name in ("amine", "amine.strategy"):
            saved_modules[mod_name] = sys.modules.pop(mod_name)

    real_import = builtins.__import__

    def _block_lgb(name, *args, **kwargs):
        if name == "lightgbm" or name.startswith("lightgbm."):
            raise ImportError("lightgbm is intentionally blocked in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_lgb)
    try:
        mod = importlib.import_module("amine.strategy")
        assert mod.AlphaMiningStrategy.version == "1.1.0"
        assert "lightgbm" not in sys.modules
    finally:
        # Restore original module state cleanly
        for mod_name in ("amine", "amine.strategy"):
            sys.modules.pop(mod_name, None)
        sys.modules.update(saved_modules)


def test_strategy_sparse_weights_explicit_zeros_and_cadence():
    from amine.strategy import AlphaMiningStrategy

    universe, dates = _make_universe(n_dates=20, symbols=("A", "B", "C", "D"))
    # First 2 rows have NaN scores; rebalance starts at bar index 2, then every k=5 bars (2, 7, 12, 17)
    scores = pd.DataFrame(np.nan, index=dates, columns=["A", "B", "C", "D"], dtype=float)
    scores.iloc[2:10] = [10.0, 8.0, 2.0, 1.0]   # top-2: A, B
    scores.iloc[10:] = [1.0, 2.0, 8.0, 10.0]    # top-2: D, C (A, B now excluded -> explicit 0.0)

    strat = AlphaMiningStrategy(score_panel=scores, top_n=2, rebalance_every=5, warmup=10)
    assert strat.version == "1.1.0"
    assert strat.warmup_bars() == 10
    assert "AlphaMiningStrategy v1.1.0" in strat.explain_weights()

    weights = strat.generate_weights(universe)
    expected_rebal_pos = {2, 7, 12, 17}

    for pos in range(len(dates)):
        row = weights.iloc[pos]
        if pos in expected_rebal_pos:
            # No NaN on rebalance rows: excluded symbols MUST be explicit 0.0
            assert row.notna().all()
            assert np.isclose(row.sum(), 1.0, atol=1e-12)
            if pos in (2, 7):
                assert np.isclose(row["A"], 0.5)
                assert np.isclose(row["B"], 0.5)
                assert row["C"] == 0.0
                assert row["D"] == 0.0
            else:
                # Previously held A, B must explicitly reset to 0.0
                assert row["A"] == 0.0
                assert row["B"] == 0.0
                assert np.isclose(row["C"], 0.5)
                assert np.isclose(row["D"], 0.5)
        else:
            # Strict sparse weights: non-rebalance days are all NaN (no ffill)
            assert row.isna().all()


def test_warmup_does_not_skip_leading_bars():
    """``warmup`` is a ``warmup_bars()``-only knob: it must NOT move the first rebalance bar.

    INTENTIONAL: ``generate_weights`` never skips leading bars from ``warmup`` because the
    score panel is pre-warmed over full history and ``common.allocation_search.run_walkforward``
    already prepends a warmup fold buffer -- skipping again would double-count the warmup.
    """
    from amine.strategy import AlphaMiningStrategy

    universe, dates = _make_universe(n_dates=20, symbols=("A", "B", "C", "D"))
    scores = pd.DataFrame(np.nan, index=dates, columns=["A", "B", "C", "D"], dtype=float)
    # First bar with finite scores is index 3; keep all later bars scored.
    scores.iloc[3:] = [4.0, 3.0, 2.0, 1.0]

    w_no_warmup = AlphaMiningStrategy(
        score_panel=scores, top_n=2, rebalance_every=4, warmup=0
    )
    w_warmup_10 = AlphaMiningStrategy(
        score_panel=scores, top_n=2, rebalance_every=4, warmup=10
    )
    assert w_warmup_10.warmup_bars() == 10

    weights_0 = w_no_warmup.generate_weights(universe)
    weights_10 = w_warmup_10.generate_weights(universe)

    expected_first = dates[3]
    assert weights_0.first_valid_index() == expected_first
    assert weights_10.first_valid_index() == expected_first
    # Identical sparse panels: warmup changes nothing in weight generation.
    pd.testing.assert_frame_equal(weights_0, weights_10)

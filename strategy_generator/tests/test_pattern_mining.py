import numpy as np
from common.testing import make_ohlcv_from_closes

from stratgen.pattern_mining import build_pattern_templates, mine_indicator_patterns


def _random_walk_universe(n=1500, vol=0.02, seed=42, n_symbols=3):
    rng = np.random.default_rng(seed)
    universe = {}
    for i in range(n_symbols):
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0, vol, n)))
        universe[chr(ord("A") + i)] = make_ohlcv_from_closes(closes, start="2020-01-01")
    return universe


def _sawtooth_universe(n_cycles=12, leg_len=80, n_symbols=3):
    # Deterministic, long-legged sawtooth: RSI/momentum-style indicators
    # should reliably still be depressed/elevated well before each
    # trough/peak (a genuine, non-noisy, well-separated pattern).
    up = np.linspace(0, 40, leg_len)
    down = np.linspace(40, 0, leg_len)
    one_cycle = np.concatenate([up, down])
    closes = 100 + np.tile(one_cycle, n_cycles)
    return {chr(ord("A") + i): make_ohlcv_from_closes(closes + i, start="2020-01-01") for i in range(n_symbols)}


def test_positive_control_planted_pattern_is_detected():
    universe = _sawtooth_universe()
    findings, status = mine_indicator_patterns(universe, seed=7, lag_bars=20)
    assert status == "ok"
    assert not findings.empty
    assert findings["significant"].sum() > 0

    templates = build_pattern_templates(findings)
    assert len(templates) > 0
    assert all(t.mined_p_value is not None and t.mined_p_value < 0.05 for t in templates)


def test_negative_control_lag_reduces_false_positives_on_pure_random_walk():
    # HONEST LIMITATION (see pattern_mining.py's module docstring): even
    # with the lag adjustment, a pure random-walk negative control can still
    # occasionally flag a handful of the menu "significant" -- the lag
    # reduces, but does not perfectly eliminate, the mechanical correlation
    # between momentum-style indicators and momentum-defined turning points.
    # This test checks the fix genuinely HELPS (fewer false positives at a
    # meaningful lag than at lag=0), rather than asserting a fragile exact
    # zero on any given seed.
    universe = _random_walk_universe(seed=42)

    findings_lag0, status0 = mine_indicator_patterns(universe, seed=7, lag_bars=0)
    findings_lag20, status20 = mine_indicator_patterns(universe, seed=7, lag_bars=20)

    assert status0 == "ok" and status20 == "ok"
    n_sig_lag0 = findings_lag0["significant"].sum()
    n_sig_lag20 = findings_lag20["significant"].sum()

    # lag=0 is expected to be badly contaminated (tautological); lag=20 must
    # be a meaningfully smaller fraction of the menu.
    assert n_sig_lag0 / len(findings_lag0) > 0.5
    assert n_sig_lag20 / len(findings_lag20) < 0.3


def test_insufficient_data_below_min_obs_floor():
    universe = _random_walk_universe(n=50, seed=1)
    findings, status = mine_indicator_patterns(universe, seed=7, pattern_min_obs=200)
    assert status == "insufficient_data"
    assert findings.empty


def test_insufficient_turning_points_below_floor():
    # Very low volatility -> almost no 5%-magnitude swings over a short series.
    universe = _random_walk_universe(n=250, vol=0.001, seed=1)
    findings, status = mine_indicator_patterns(
        universe, seed=7, min_swing_pct=0.05, pattern_min_obs=200, pattern_min_turning_points=20
    )
    assert status == "insufficient_turning_points"
    assert findings.empty


def test_build_pattern_templates_returns_empty_list_for_empty_findings():
    import pandas as pd
    assert build_pattern_templates(pd.DataFrame()) == []


def test_build_pattern_templates_respects_max_templates_cap():
    universe = _sawtooth_universe()
    findings, _ = mine_indicator_patterns(universe, seed=7, lag_bars=20)
    templates = build_pattern_templates(findings, max_templates=2)
    assert len(templates) <= 2

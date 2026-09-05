"""Golden-master regression test, written BEFORE `pattern_mining.mine_indicator_patterns`
was refactored to build on the shared `common.significance.shuffle_null_test`
primitive. Values in golden_pattern_mining_values.json were captured by
running the exact fixtures/seeds below against the pre-refactor
implementation; comparing the full findings DataFrame column-by-column with
tight floating-point tolerance (`pytest.approx` with `rel=1e-9, abs=1e-12`)
verifies the refactor matches across numerical platforms while avoiding machine epsilon
fragility. The negative-
control (random-walk) fixture exercises many more (feature, event_type)
combinations than the positive-control fixture and is the more sensitive
check for a reference/alpha mixup. Guaranteed 100% offline/synthetic.
"""

import json
import os

import numpy as np
import pytest
from common.testing import make_ohlcv_from_closes

from pmine.pattern_mining import mine_indicator_patterns

_GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_pattern_mining_values.json")
with open(_GOLDEN_PATH) as _f:
    GOLDEN = json.load(_f)


def _random_walk_universe(n=1500, vol=0.02, seed=42, n_symbols=3):
    rng = np.random.default_rng(seed)
    universe = {}
    for i in range(n_symbols):
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0, vol, n)))
        universe[chr(ord("A") + i)] = make_ohlcv_from_closes(closes, start="2020-01-01")
    return universe


def _sawtooth_universe(n_cycles=12, leg_len=80, n_symbols=3):
    up = np.linspace(0, 40, leg_len)
    down = np.linspace(40, 0, leg_len)
    one_cycle = np.concatenate([up, down])
    closes = 100 + np.tile(one_cycle, n_cycles)
    return {chr(ord("A") + i): make_ohlcv_from_closes(closes + i, start="2020-01-01") for i in range(n_symbols)}


def _assert_findings_match_golden(findings, golden_dict):
    assert list(findings.columns) == list(golden_dict.keys())
    for col in golden_dict:
        golden_values = golden_dict[col]
        actual_values = findings[col].tolist()
        assert len(actual_values) == len(golden_values), f"column {col}: length mismatch"
        for actual, expected in zip(actual_values, golden_values):
            if isinstance(expected, float):
                assert actual == pytest.approx(expected, rel=1e-9, abs=1e-12), f"column {col}: expected {expected!r}, got {actual!r}"
            elif isinstance(expected, list):
                assert list(actual) == expected, f"column {col}: expected {expected!r}, got {actual!r}"
            else:
                assert actual == expected, f"column {col}: expected {expected!r}, got {actual!r}"


def test_mine_indicator_patterns_matches_golden_random_walk():
    universe = _random_walk_universe(seed=42)
    findings, status = mine_indicator_patterns(universe, seed=7, lag_bars=20)
    assert status == GOLDEN["pattern_mining_rw_status"]
    _assert_findings_match_golden(findings, GOLDEN["pattern_mining_rw_findings"])


def test_mine_indicator_patterns_matches_golden_sawtooth():
    universe = _sawtooth_universe()
    findings, status = mine_indicator_patterns(universe, seed=7, lag_bars=20)
    assert status == GOLDEN["pattern_mining_saw_status"]
    _assert_findings_match_golden(findings, GOLDEN["pattern_mining_saw_findings"])

"""Unit tests for `common/sleeve_allocator.py` (M2.2 Core-Satellite SleeveAllocator).

100% OFFLINE: synthetic OHLCV only, zero network calls.

Covers the acceptance contract:
1. hand-computable fixed-budget split (0.25/2 beta, 0.55/2 alpha),
2. the `sum(budgets) == 1` hard error,
3. the crisis overlay touching ONLY the beta sleeve,
4. `sum(risky) <= 1` plus an explicit cash leg,
5. the sparse-weights contract (NaN off-schedule, explicit 0.0 for excluded
   symbols, never forward-filled),
6. sleeve migration between rebalance dates (explicit numbers on both sides),
7. `inverse_vol` within-sleeve sizing vs. the `equal` default,
8. version, 9. registry membership, 10. import-order registration regression
   (fresh-interpreter probe in both import orders).
"""

import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_templates import ALLOCATION_TEMPLATES
from common.sleeve_allocator import SleeveAllocator
from common.testing import make_ohlcv_from_closes


def _random_walk_universe(symbols, n=250, seed=0, start="2020-01-01", vol=1.0):
    rng = np.random.default_rng(seed)
    return {
        sym: make_ohlcv_from_closes(100.0 + np.cumsum(rng.normal(0.05, vol, n)), start=start)
        for sym in symbols
    }


def _rebalance_rows(frame):
    return frame[frame.notna().any(axis=1)]


def _four_symbol_universe(seed=0):
    return _random_walk_universe(["B1", "B2", "A1", "A2"], seed=seed)


SPLIT_PARAMS = {
    "rebalance_freq_days": 21,
    "sleeve_beta_tickers": ["B1", "B2"],
    "sleeve_alpha_tickers": ["A1", "A2"],
}


# --------------------------------------------------------------------------- #
# 1. Hand-computable split
# --------------------------------------------------------------------------- #
def test_hand_computable_sleeve_split():
    """2 beta + 2 alpha, equal weights within each sleeve, fixed budgets =>
    exactly 0.25/2 and 0.55/2 on every scheduled rebalance row."""
    template = SleeveAllocator()
    weights = template.generate_weights(_four_symbol_universe(), SPLIT_PARAMS)

    rows = _rebalance_rows(weights)
    assert len(rows) > 1  # the schedule really produced several rebalances

    for row in rows.to_numpy():
        np.testing.assert_allclose(row, [0.125, 0.125, 0.275, 0.275], rtol=0, atol=1e-12)


# --------------------------------------------------------------------------- #
# 2. Budget assertion
# --------------------------------------------------------------------------- #
def test_budgets_must_sum_to_one():
    template = SleeveAllocator()
    bad_params = dict(SPLIT_PARAMS)
    bad_params.update(
        {"sleeve_beta_budget": 0.25, "sleeve_alpha_budget": 0.55, "sleeve_cash_budget": 0.30},
    )
    with pytest.raises(ValueError, match="must sum to exactly 1.0"):
        template.generate_weights(_four_symbol_universe(), bad_params)

    # A negative budget is likewise a configuration error, not a degrade.
    with pytest.raises(ValueError, match="non-negative"):
        template.generate_weights(
            _four_symbol_universe(),
            {**SPLIT_PARAMS, "sleeve_beta_budget": -0.1, "sleeve_cash_budget": 0.30},
        )


# --------------------------------------------------------------------------- #
# 3. Crisis overlay: beta only
# --------------------------------------------------------------------------- #
def test_crisis_overlay_halves_beta_and_leaves_alpha_bit_identical():
    template = SleeveAllocator()
    universe = _four_symbol_universe(seed=3)

    calm = template.generate_weights(universe, SPLIT_PARAMS)
    crisis = template.generate_weights(universe, {**SPLIT_PARAMS, "sleeve_crisis_active": True})

    calm_rows, crisis_rows = _rebalance_rows(calm), _rebalance_rows(crisis)
    pd.testing.assert_index_equal(calm_rows.index, crisis_rows.index)

    # Beta sleeve (0.125 each) is scaled by sleeve_crisis_scale=0.5.
    np.testing.assert_allclose(
        crisis_rows[["B1", "B2"]].to_numpy(),
        calm_rows[["B1", "B2"]].to_numpy() * 0.5,
        rtol=0, atol=1e-12,
    )
    # Alpha sleeve is untouched -- bit-identical, not merely close.
    assert np.array_equal(crisis_rows[["A1", "A2"]].to_numpy(), calm_rows[["A1", "A2"]].to_numpy())
    assert (crisis_rows[["A1", "A2"]].to_numpy() == 0.275).all()

    # Budget discipline survives the overlay.
    assert (crisis_rows.sum(axis=1) <= 1.0 + 1e-12).all()


def test_crisis_overlay_accepts_a_date_keyed_regime_series():
    """The crisis hook is caller-supplied and can switch mid-sample: only the
    flagged rebalance dates get the beta scale."""
    template = SleeveAllocator()
    universe = _four_symbol_universe(seed=4)
    base = template.generate_weights(universe, SPLIT_PARAMS)
    dates = _rebalance_rows(base).index
    assert len(dates) >= 3

    regime = pd.Series([False] * len(dates), index=dates, dtype=bool)
    regime.iloc[1] = True
    weights = template.generate_weights(universe, {**SPLIT_PARAMS, "sleeve_crisis_active": regime})
    rows = _rebalance_rows(weights)

    assert rows.loc[dates[0], "B1"] == pytest.approx(0.125, abs=1e-12)
    assert rows.loc[dates[1], "B1"] == pytest.approx(0.0625, abs=1e-12)
    assert rows.loc[dates[1], "A1"] == pytest.approx(0.275, abs=1e-12)
    assert rows.loc[dates[2], "B1"] == pytest.approx(0.125, abs=1e-12)


# --------------------------------------------------------------------------- #
# 4. sum <= 1 and the explicit cash leg
# --------------------------------------------------------------------------- #
def test_risky_sum_within_budget_and_cash_leg_written_explicitly():
    template = SleeveAllocator()
    universe = _four_symbol_universe(seed=5)
    universe["CASH"] = make_ohlcv_from_closes(np.full(250, 100.0))
    params = {**SPLIT_PARAMS, "sleeve_cash_ticker": "CASH"}

    weights = template.generate_weights(universe, params)
    rows = _rebalance_rows(weights)

    risky = rows[["B1", "B2", "A1", "A2"]].sum(axis=1)
    np.testing.assert_allclose(risky.to_numpy(), 0.80, rtol=0, atol=1e-12)
    assert (rows.sum(axis=1) <= 1.0 + 1e-12).all()
    # The 0.20 cash budget is written into the designated ticker, explicitly.
    np.testing.assert_allclose(rows["CASH"].to_numpy(), 0.20, rtol=0, atol=1e-12)

    # Without a designated cash ticker the remainder is implicit (risky <= 1)
    # and no stray cash column is invented.
    implicit = _rebalance_rows(template.generate_weights(_four_symbol_universe(seed=5), SPLIT_PARAMS))
    np.testing.assert_allclose(implicit.sum(axis=1).to_numpy(), 0.80, rtol=0, atol=1e-12)


# --------------------------------------------------------------------------- #
# 5. Sparse-weights contract
# --------------------------------------------------------------------------- #
def test_sparse_contract_off_schedule_nan_and_excluded_symbol_explicit_zero():
    template = SleeveAllocator()
    universe = _four_symbol_universe(seed=6)
    universe["OUTSIDER"] = make_ohlcv_from_closes(
        100.0 + np.cumsum(np.random.default_rng(11).normal(0.05, 1.0, 250))
    )

    weights = template.generate_weights(universe, SPLIT_PARAMS)

    rebalance_mask = weights.notna().any(axis=1)
    assert rebalance_mask.sum() > 1
    assert (~rebalance_mask).sum() > 1
    # Every off-schedule row is fully NaN -- the template never ffill()s.
    assert weights.loc[~rebalance_mask].isna().all().all()
    assert not weights.equals(weights.ffill())

    # On EVERY rebalance row every symbol carries a real number, and the symbol
    # in neither sleeve is an explicit 0.0 (not NaN, which the backtester's
    # column-wise ffill would silently turn into a stale carried position).
    rows = weights.loc[rebalance_mask]
    assert not rows.isna().any().any()
    assert (rows["OUTSIDER"] == 0.0).all()

    # And the excluded name can never be resurrected by ffill on the full frame.
    assert (weights["OUTSIDER"].dropna() == 0.0).all()


def test_no_zero_weight_is_left_as_nan_inside_a_rebalance_row():
    """Directly pins the NaN-vs-0.0 rule: after the backtester's own
    `ffill().fillna(0.0)` the excluded symbol must stay 0.0 for the whole
    sample, which only holds if the rebalance row wrote a real 0.0."""
    template = SleeveAllocator()
    universe = _four_symbol_universe(seed=7)
    universe["OUTSIDER"] = make_ohlcv_from_closes(np.full(250, 50.0))

    weights = template.generate_weights(universe, SPLIT_PARAMS)
    carried = weights.ffill().fillna(0.0)
    assert (carried["OUTSIDER"] == 0.0).all()


# --------------------------------------------------------------------------- #
# 6. Sleeve migration between rebalances
# --------------------------------------------------------------------------- #
def test_sleeve_migration_writes_explicit_numbers_on_both_sides():
    """X moves beta -> alpha between two rebalances: both rows must carry a real
    number for X (0.25/2 = 0.125 while core, 0.55/2 = 0.275 once satellite),
    and the name it replaced must not be left carrying a stale weight."""
    template = SleeveAllocator()
    universe = _random_walk_universe(["CORE", "X", "SAT"], seed=8)
    base = template.generate_weights(universe, {"rebalance_freq_days": 21})
    dates = _rebalance_rows(base).index
    assert len(dates) >= 3
    first, third = dates[0], dates[2]

    params = {
        "rebalance_freq_days": 21,
        "sleeve_beta_tickers": {str(first): ["CORE", "X"], str(third): ["CORE"]},
        "sleeve_alpha_tickers": {str(first): ["SAT"], str(third): ["SAT", "X"]},
    }
    weights = template.generate_weights(universe, params)
    rows = weights.loc[[first, third]]

    assert not rows.isna().any().any()
    np.testing.assert_allclose(rows.loc[first].to_numpy(), [0.125, 0.125, 0.55], rtol=0, atol=1e-12)
    np.testing.assert_allclose(rows.loc[third].to_numpy(), [0.25, 0.275, 0.275], rtol=0, atol=1e-12)

    # Intermediate (non-scheduled) dates stay NaN, so nothing is carried; the
    # middle scheduled rebalance still applies the LAST STATED membership
    # (a schedule is a step function, not a one-date override).
    middle = dates[1]
    assert weights.loc[middle].notna().all()
    np.testing.assert_allclose(weights.loc[middle].to_numpy(), [0.125, 0.125, 0.55], rtol=0, atol=1e-12)
    off_schedule = weights.loc[first:third].drop(index=[first, middle, third])
    assert len(off_schedule) > 1
    assert off_schedule.isna().all().all()

    # A symbol in BOTH sleeves on the same date is a configuration error.
    with pytest.raises(ValueError, match="cannot sit in both"):
        template.generate_weights(
            universe,
            {
                "rebalance_freq_days": 21,
                "sleeve_beta_tickers": ["CORE", "X"],
                "sleeve_alpha_tickers": ["X"],
            },
        )


# --------------------------------------------------------------------------- #
# 7. Within-sleeve weighting
# --------------------------------------------------------------------------- #
def test_inverse_vol_within_sleeve_downweights_the_volatile_member():
    template = SleeveAllocator()
    n = 250
    dates = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(9)
    calm = 100.0 + np.cumsum(rng.normal(0.05, 0.2, n))
    wild = 100.0 + np.cumsum(rng.normal(0.05, 3.0, n))
    universe = {
        "CALM": make_ohlcv_from_closes(calm),
        "WILD": make_ohlcv_from_closes(wild),
        "SAT": make_ohlcv_from_closes(100.0 + np.cumsum(rng.normal(0.05, 1.0, n))),
    }
    params = {
        "rebalance_freq_days": 21,
        "sleeve_beta_tickers": ["CALM", "WILD"],
        "sleeve_alpha_tickers": ["SAT"],
        "sleeve_within_weighting": "inverse_vol",
        "sleeve_vol_lookback": 60,
    }
    weights = template.generate_weights(universe, params)
    rows = _rebalance_rows(weights)
    # The first rebalance dates fall inside the realized-vol warmup: the sleeve
    # has no vol estimate yet, so it holds cash (0.0) rather than guessing.
    rows = rows[rows.sum(axis=1) > 0.0]
    assert len(rows) > 1

    assert (rows["CALM"] > rows["WILD"]).all(), "higher-vol beta member must get less weight"
    np.testing.assert_allclose(rows["CALM"] + rows["WILD"], 0.25, rtol=0, atol=1e-12)
    np.testing.assert_allclose(rows["SAT"], 0.55, rtol=0, atol=1e-12)

    # `equal` is the DEFAULT path (no sleeve_within_weighting in params).
    default_rows = _rebalance_rows(template.generate_weights(_four_symbol_universe(seed=9), SPLIT_PARAMS))
    assert not default_rows.empty
    np.testing.assert_allclose(default_rows["B1"].to_numpy(), 0.125, rtol=0, atol=1e-12)
    assert SleeveAllocator().warmup_bars({"sleeve_within_weighting": "equal"}) == 0
    assert SleeveAllocator().warmup_bars(
        {"sleeve_within_weighting": "inverse_vol", "sleeve_vol_lookback": 60}
    ) == 60


# --------------------------------------------------------------------------- #
# 8. Version
# --------------------------------------------------------------------------- #
def test_sleeve_allocator_version():
    assert SleeveAllocator().version == "1.0.0"
    assert SleeveAllocator().version_string == "v1.0.0"


# --------------------------------------------------------------------------- #
# 9. Registry
# --------------------------------------------------------------------------- #
def test_sleeve_allocator_is_registered_as_the_tenth_static_template():
    assert SleeveAllocator in ALLOCATION_TEMPLATES
    assert len(ALLOCATION_TEMPLATES) == 10
    assert [cls for cls in ALLOCATION_TEMPLATES if cls.name == "sleeve_allocator"] == [SleeveAllocator]
    # Zero-arg constructible, per the registry's contract.
    assert SleeveAllocator().name == "sleeve_allocator"
    assert SleeveAllocator().factor_tags


# Runs in a FRESH interpreter so each import order is exercised from a genuinely
# empty `sys.modules` -- the in-process suite above cannot do that, because the
# first import already bound both modules.
_IMPORT_ORDER_PROBE = r"""
import json
import sys

order = sys.argv[1]
if order == "registry_first":
    from common.allocation_templates import ALLOCATION_TEMPLATES
    from common.sleeve_allocator import SleeveAllocator
else:
    from common.sleeve_allocator import SleeveAllocator
    from common.allocation_templates import ALLOCATION_TEMPLATES

print(json.dumps({
    "sleeve_is_registered": SleeveAllocator in ALLOCATION_TEMPLATES,
    "sleeve_hits": sum(1 for cls in ALLOCATION_TEMPLATES if cls is SleeveAllocator),
    "total": len(ALLOCATION_TEMPLATES),
    "names": [cls.name for cls in ALLOCATION_TEMPLATES],
}))
"""


def test_registry_registers_sleeve_exactly_once_under_both_import_orders():
    """`common/sleeve_allocator.py` imports the base class from
    `common/allocation_templates.py`, which appends `SleeveAllocator` to
    `ALLOCATION_TEMPLATES` behind a `try/except ImportError` at its own bottom.
    That is a real circular-import hazard: the append is a no-op in one order and
    the allocator self-registers in the other. Only if BOTH branches are guarded
    correctly does the registry end up with the same single entry either way, so
    each order is probed in a fresh subprocess (a separate interpreter, not just a
    `sys.modules` cleanup, since partially-initialized modules would otherwise leak).
    """
    failures = []
    for order in ("registry_first", "allocator_first"):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [_PROJECT_ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        res = subprocess.run(
            [sys.executable, "-c", _IMPORT_ORDER_PROBE, order],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=env,
        )
        assert res.returncode == 0, f"import order {order!r} failed: {res.stderr}"
        payload = json.loads(res.stdout.strip().splitlines()[-1])

        assert payload["sleeve_is_registered"], f"{order}: SleeveAllocator missing from registry"
        assert payload["sleeve_hits"] == 1, f"{order}: registered {payload['sleeve_hits']}x"
        assert payload["total"] == 10, f"{order}: registry has {payload['total']} entries"
        names = payload["names"]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, f"{order}: duplicate template names {duplicates}"
        assert len(names) == payload["total"], f"{order}: name/total mismatch"

        failures.append((order, payload["total"], payload["sleeve_hits"]))
    assert failures == [("registry_first", 10, 1), ("allocator_first", 10, 1)]


def test_explanation_and_param_grid_are_complete():
    template = SleeveAllocator()
    text = template.explain_weights({"rebalance_freq_days": 21})
    assert "Core-Satellite" in text
    assert "25%" in text and "55%" in text and "20%" in text
    assert "rebalance_freq_days" in template.param_grid
    assert "equal" in template.param_grid["sleeve_within_weighting"]

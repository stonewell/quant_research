"""Offline regression suite for `common/brinson_attribution.py` (development_plan `AT1`).

`AT1` (docs/development_plan.md §2.24, line 407) asks for a pure Brinson
allocation/selection/interaction attribution function with a per-sector report,
a 1e-12 identity, an explicit `UNCLASSIFIED` bucket for assets with no sector,
advisory-only wiring, and reuse of `common/sector_metadata.load_sector_map` /
`resolve_sector` -- never a second sector mapping table.

100% offline: every fixture below is hand-written or generated from a seeded
`numpy` RNG plus `common.testing.assign_synthetic_sectors`. No network, no real
market data, no file writes outside `tmp_path`.

Numerical discipline: the identity is asserted against `atol=1e-12` directly
(NOT `pytest.approx`, which defaults to 1e-6 relative and would hide drift).
"""

import copy
import inspect
import json
import os
import sys
from collections.abc import Mapping

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pytest

from common import brinson_attribution as brinson_mod
from common import sector_metadata
from common.brinson_attribution import (
    UNCLASSIFIED,
    BrinsonAttribution,
    SectorContribution,
    brinson_attribution,
)
from common.testing import assign_synthetic_sectors

ATOL = 1e-12


def _identity_gap(result: BrinsonAttribution) -> float:
    """|allocation + selection + interaction - active_return| recomputed here."""
    return abs(
        result.allocation_effect
        + result.selection_effect
        + result.interaction_effect
        - result.active_return
    )


def _assert_identity(result: BrinsonAttribution) -> None:
    assert _identity_gap(result) <= ATOL
    assert abs(result.residual) <= ATOL


def _random_book(seed: int, n: int):
    """Seeded synthetic weights/returns; weights are normalised, returns ~N(0, 3%)."""
    rng = np.random.default_rng(seed)
    symbols = [f"SYN{i:03d}" for i in range(n)]
    raw_p = rng.random(n) + 0.05
    raw_b = rng.random(n) + 0.05
    pw = dict(zip(symbols, raw_p / raw_p.sum()))
    bw = dict(zip(symbols, raw_b / raw_b.sum()))
    rets = dict(zip(symbols, rng.normal(0.0, 0.03, n)))
    return symbols, pw, bw, rets


# --------------------------------------------------------------------------- #
# 1. The identity: allocation + selection + interaction == active return
# --------------------------------------------------------------------------- #
def test_identity_holds_on_synthetic_multi_sector_universe():
    symbols, pw, bw, rets = _random_book(seed=7, n=24)
    sector_map = assign_synthetic_sectors(symbols, n_sectors=4, seed=3)

    result = brinson_attribution(pw, bw, rets, sector_map)

    # Independently recomputed active return, from the raw inputs.
    expected_active = sum(pw[s] * rets[s] for s in symbols) - sum(bw[s] * rets[s] for s in symbols)
    assert abs(result.active_return - expected_active) <= ATOL
    assert abs(result.portfolio_return - sum(pw[s] * rets[s] for s in symbols)) <= ATOL
    assert abs(result.benchmark_return - sum(bw[s] * rets[s] for s in symbols)) <= ATOL
    _assert_identity(result)
    # A real universe must actually exercise more than one sector.
    assert len(result.sectors) >= 2


def test_identity_holds_for_several_seeds_and_unbalanced_books():
    """Weights that do NOT sum to 1 (cash drag) and shorts must still close exactly."""
    for seed in (0, 11, 29):
        symbols, pw, bw, rets = _random_book(seed=seed, n=12)
        # Leave 15% in cash and short one name in the portfolio book.
        pw = {s: w * 0.85 for s, w in pw.items()}
        short = symbols[0]
        pw[short] = -0.10
        sector_map = assign_synthetic_sectors(symbols, n_sectors=3, seed=seed + 1)

        result = brinson_attribution(pw, bw, rets, sector_map)

        assert abs(sum(pw[s] * rets[s] for s in symbols) - result.portfolio_return) <= ATOL
        _assert_identity(result)


# --------------------------------------------------------------------------- #
# 2. Per-sector rows must roll up to the three aggregate effects
# --------------------------------------------------------------------------- #
def test_sector_rows_roll_up_to_aggregate_effects():
    symbols, pw, bw, rets = _random_book(seed=101, n=18)
    sector_map = assign_synthetic_sectors(symbols, n_sectors=5, seed=5)

    result = brinson_attribution(pw, bw, rets, sector_map)

    alloc = sum(row.allocation_effect for row in result.sectors)
    sel = sum(row.selection_effect for row in result.sectors)
    inter = sum(row.interaction_effect for row in result.sectors)

    assert abs(alloc - result.allocation_effect) <= ATOL
    assert abs(sel - result.selection_effect) <= ATOL
    assert abs(inter - result.interaction_effect) <= ATOL

    # Each row is internally consistent: its three effects sum to its own
    # active contribution, so the report can be read sector by sector.
    for row in result.sectors:
        own_active = row.portfolio_weight * row.portfolio_return - (
            row.benchmark_weight * row.benchmark_return
        )
        assert abs(row.total_effect - own_active) <= ATOL, row
        # ... and its total also equals sum_i w_p,i r_i - sum_i w_b,i r_i over
        # exactly the members of that sector (checked against the raw inputs).
        members = [s for s in symbols if sector_map[s] == row.sector]
        exp = sum(pw[s] * rets[s] for s in members) - sum(bw[s] * rets[s] for s in members)
        assert abs(row.total_effect - exp) <= ATOL, row

    # The report is deterministic in ordering (sorted sector labels).
    assert [row.sector for row in result.sectors] == sorted(row.sector for row in result.sectors)

    frame = result.to_dataframe()
    assert list(frame.index) == [row.sector for row in result.sectors]
    assert abs(frame["allocation_effect"].sum() - result.allocation_effect) <= ATOL


# --------------------------------------------------------------------------- #
# 3. Hand-computed two-sector example (allocation vs selection, no interaction)
# --------------------------------------------------------------------------- #
def test_hand_computed_two_sector_example():
    """tech: 0.30/0.00 portfolio vs 0.20/0.40 benchmark; energy: identical both sides.

    tech portfolio return = (0.30*0.20 + 0.30*0.00) / 0.60 = 0.10
    tech benchmark return = (0.20*0.20 + 0.40*0.00) / 0.60 = 0.04/0.6 = 1/15
    energy: w and r identical on both sides -> zero contribution

    allocation(tech)  = (0.60 - 0.60) * 1/15 = 0
    selection(tech)   = 0.60 * (0.10 - 1/15) = 0.02
    interaction(tech) = 0
    portfolio return  = 0.30*0.20 + 0.30*0.00 + 0.40*(-0.10) = +0.020
    benchmark return  = 0.20*0.20 + 0.40*0.00 + 0.40*(-0.10) = +0.000
    """
    pw = {"AAA": 0.30, "BBB": 0.30, "CCC": 0.40}
    bw = {"AAA": 0.20, "BBB": 0.40, "CCC": 0.40}
    rets = {"AAA": 0.20, "BBB": 0.00, "CCC": -0.10}
    sector_map = {"AAA": "tech", "BBB": "tech", "CCC": "energy"}

    result = brinson_attribution(pw, bw, rets, sector_map)

    tech = result.sector("tech")
    energy = result.sector("energy")

    assert tech.portfolio_weight == pytest.approx(0.60, abs=ATOL)
    assert tech.benchmark_weight == pytest.approx(0.60, abs=ATOL)
    assert tech.portfolio_return == pytest.approx(0.10, abs=ATOL)
    assert tech.benchmark_return == pytest.approx(1.0 / 15.0, abs=ATOL)
    assert abs(tech.allocation_effect - 0.0) <= ATOL
    assert abs(tech.selection_effect - 0.02) <= ATOL
    assert abs(tech.interaction_effect - 0.0) <= ATOL

    assert abs(energy.allocation_effect) <= ATOL
    assert abs(energy.selection_effect) <= ATOL
    assert abs(energy.interaction_effect) <= ATOL
    assert abs(energy.total_effect) <= ATOL

    assert abs(result.portfolio_return - 0.02) <= ATOL
    assert abs(result.benchmark_return - 0.0) <= ATOL
    assert abs(result.active_return - 0.02) <= ATOL
    assert abs(result.selection_effect - 0.02) <= ATOL
    assert abs(result.allocation_effect) <= ATOL
    assert abs(result.interaction_effect) <= ATOL
    _assert_identity(result)


# --------------------------------------------------------------------------- #
# 4. Missing sectors -> explicit UNCLASSIFIED bucket, weights conserved
# --------------------------------------------------------------------------- #
def test_missing_sector_assets_land_in_unclassified_and_weights_are_conserved():
    pw = {"AAA": 0.40, "BBB": 0.25, "ZZZ": 0.35}
    bw = {"AAA": 0.30, "BBB": 0.40, "ZZZ": 0.30}
    rets = {"AAA": 0.05, "BBB": -0.02, "ZZZ": 0.12}
    # ZZZ deliberately absent from the supplied map.
    sector_map = {"AAA": "tech", "BBB": "energy"}

    result = brinson_attribution(pw, bw, rets, sector_map)

    assert result.unclassified_symbols == ("ZZZ",)
    unclassified_row = result.sector(UNCLASSIFIED)
    assert unclassified_row.portfolio_weight == pytest.approx(0.35, abs=ATOL)
    assert unclassified_row.benchmark_weight == pytest.approx(0.30, abs=ATOL)
    assert unclassified_row.portfolio_return == pytest.approx(0.12, abs=ATOL)

    # No silent drop: every symbol's weight is still accounted for in the report.
    assert abs(sum(r.portfolio_weight for r in result.sectors) - sum(pw.values())) <= ATOL
    assert abs(sum(r.benchmark_weight for r in result.sectors) - sum(bw.values())) <= ATOL

    # And the unclassified asset's own contribution is in the report, not lost.
    assert abs(
        unclassified_row.total_effect - (0.35 * 0.12 - 0.30 * 0.12)
    ) <= ATOL
    _assert_identity(result)

    # No sector-mean imputation: the bucket return is the asset's own return.
    assert unclassified_row.portfolio_return != pytest.approx(
        (0.05 + (-0.02)) / 2.0, abs=1e-9
    )

    # The disclosure is visible in the human-readable report too.
    assert "UNCLASSIFIED" in result.summary()
    assert "ZZZ" in result.summary()


def test_supplied_map_is_authoritative_but_no_map_uses_resolve_sector_fallback():
    """Documented policy: a supplied map wins over the heuristic; no map -> heuristic."""
    pw = {"601288.SH": 0.5, "999999.SZ": 0.5}
    bw = {"601288.SH": 0.5, "999999.SZ": 0.5}
    rets = {"601288.SH": 0.01, "999999.SZ": 0.02}

    # Map supplied, only one symbol covered -> the other is UNCLASSIFIED even
    # though fallback='auto' could have invented 'sec_99'.
    strict = brinson_attribution(pw, bw, rets, {"601288.SH": "banking"}, fallback="auto")
    assert strict.unclassified_symbols == ("999999.SZ",)
    assert set(r.sector for r in strict.sectors) == {"banking", UNCLASSIFIED}

    # No map at all -> resolve_sector's prefix heuristic is honoured.
    heuristic = brinson_attribution(pw, bw, rets, None, fallback="auto")
    assert heuristic.unclassified_symbols == ()
    assert set(r.sector for r in heuristic.sectors) == {"sec_60", "sec_99"}

    # Blank / NaN-valued map entries == no information -> UNCLASSIFIED (never a
    # fake label produced by the heuristic fallback).
    blank = brinson_attribution(pw, bw, rets, {"601288.SH": "  ", "999999.SZ": "nan"})
    assert set(blank.unclassified_symbols) == {"601288.SH", "999999.SZ"}
    assert len(blank.sectors) == 1 and blank.sectors[0].sector == UNCLASSIFIED
    _assert_identity(blank)


# --------------------------------------------------------------------------- #
# 5. Reuse of common.sector_metadata.resolve_sector (spy) / no private mapping
# --------------------------------------------------------------------------- #
def test_resolve_sector_is_called_for_every_symbol(monkeypatch):
    symbols = [f"S{i}" for i in range(6)]
    pw = dict(zip(symbols, [0.2] * 5 + [-0.0]))
    bw = dict(zip(symbols, [1.0 / 6.0] * 6))
    rets = dict(zip(symbols, [0.01 * i for i in range(6)]))

    calls = []
    real_resolve = sector_metadata.resolve_sector

    def spy(symbol, sector_map=None, *, fallback="auto"):
        calls.append((symbol, fallback))
        return real_resolve(symbol, sector_map, fallback=fallback)

    monkeypatch.setattr(brinson_mod, "resolve_sector", spy)

    result = brinson_attribution(pw, bw, rets, None, fallback="prefix")

    assert [c[0] for c in calls] == symbols  # every symbol, exactly once
    assert all(c[1] == "prefix" for c in calls)  # caller's fallback forwarded
    assert set(r.sector for r in result.sectors)  # labels came from the spy
    _assert_identity(result)


def test_module_declares_no_private_sector_mapping_table():
    """`AT1` forbids a second sector map: no module-level mapping/table allowed."""
    public_state = {k: v for k, v in vars(brinson_mod).items() if not k.startswith("__")}
    offenders = {k: type(v).__name__ for k, v in public_state.items() if isinstance(v, Mapping)}
    assert offenders == {}, f"unexpected module-level mapping(s): {offenders}"

    # The sector taxonomy/fallback list is re-exported by identity, not copied.
    assert brinson_mod.VALID_FALLBACKS is sector_metadata.VALID_FALLBACKS
    # Function references are the originals from sector_metadata.
    assert brinson_mod.resolve_sector is sector_metadata.resolve_sector
    assert brinson_mod.load_sector_map is sector_metadata.load_sector_map


def test_load_sector_map_is_used_for_path_inputs(tmp_path, monkeypatch):
    path = tmp_path / "sector_map.json"
    path.write_text(
        json.dumps({"_meta": {"note": "offline fixture"}, "AAA": "tech", "bbb": "energy"}),
        encoding="utf-8",
    )
    pw = {"AAA": 0.5, "BBB": 0.5}
    bw = {"AAA": 0.5, "BBB": 0.5}
    rets = {"AAA": 0.02, "BBB": 0.01}

    calls = []
    real_loader = sector_metadata.load_sector_map

    def spy(loader_path=None):
        calls.append(loader_path)
        return real_loader(loader_path)

    monkeypatch.setattr(brinson_mod, "load_sector_map", spy)

    result = brinson_attribution(pw, bw, rets, str(path))

    assert calls == [str(path)]
    assert result.unclassified_symbols == ()
    assert set(r.sector for r in result.sectors) == {"tech", "energy"}
    _assert_identity(result)


# --------------------------------------------------------------------------- #
# 6. Purity: repeat calls identical, inputs untouched, no I/O
# --------------------------------------------------------------------------- #
def test_repeated_calls_are_identical_and_inputs_are_not_mutated():
    symbols, pw, bw, rets = _random_book(seed=42, n=10)
    sector_map = assign_synthetic_sectors(symbols, n_sectors=3, seed=2)

    pw_before = copy.deepcopy(pw)
    bw_before = copy.deepcopy(bw)
    rets_before = copy.deepcopy(rets)
    map_before = copy.deepcopy(sector_map)

    first = brinson_attribution(pw, bw, rets, sector_map)
    second = brinson_attribution(pw, bw, rets, sector_map)

    assert first == second  # frozen dataclasses: structural equality
    assert first.residual == second.residual  # bit-identical, no RNG/state
    assert first.sectors == second.sectors
    assert first.as_dict() == second.as_dict()
    assert (pw, bw, rets, sector_map) == (pw_before, bw_before, rets_before, map_before)
    _assert_identity(first)


def test_module_is_advisory_only_and_imports_no_execution_path():
    """No gate / order-path / network / subprocess import may exist in the module."""
    import ast

    tree = ast.parse(inspect.getsource(brinson_mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)

    forbidden_prefixes = (
        "scripts",
        "web",
        "pipeline.research.blend",
        "subprocess",
        "requests",
        "urllib",
        "socket",
        "http",
    )
    for module in imported:
        for prefix in forbidden_prefixes:
            assert not (module == prefix or module.startswith(prefix + ".")), (
                f"advisory module must not import {module!r}"
            )
    # The only project imports are the sector-metadata single source of truth.
    project_imports = {m for m in imported if m.split(".")[0] == "common"}
    assert project_imports == {"common.sector_metadata"}


# --------------------------------------------------------------------------- #
# 7. Degenerate universes: all-cash, single asset, one-sided buckets, NaN returns
# --------------------------------------------------------------------------- #
def test_all_cash_portfolio_is_exactly_zero_when_benchmark_is_also_flat():
    pw = {"AAA": 0.0, "BBB": 0.0}
    bw = {"AAA": 0.0, "BBB": 0.0}
    rets = {"AAA": 0.05, "BBB": -0.03}

    result = brinson_attribution(pw, bw, rets, {"AAA": "a", "BBB": "b"})

    assert result.portfolio_return == 0.0
    assert result.benchmark_return == 0.0
    assert result.allocation_effect == 0.0
    assert result.selection_effect == 0.0
    assert result.interaction_effect == 0.0
    assert result.residual == 0.0
    _assert_identity(result)


def test_all_cash_portfolio_against_invested_benchmark_charges_allocation():
    """Zero portfolio weight, invested benchmark: active == -r_b exactly."""
    pw = {"AAA": 0.0, "BBB": 0.0}
    bw = {"AAA": 0.5, "BBB": 0.5}
    rets = {"AAA": 0.10, "BBB": 0.00}

    result = brinson_attribution(pw, bw, rets, {"AAA": "a", "BBB": "b"})

    assert abs(result.active_return - (-0.05)) <= ATOL
    # Allocation owns the whole miss: (0 - w_b,s) * r_b,s summed.
    assert abs(result.allocation_effect - (-0.05)) <= ATOL
    # With r_p identically 0, selection cancels allocation and interaction
    # restores it -- the decomposition stays exact for the degenerate book.
    assert abs(result.selection_effect - (-0.05)) <= ATOL
    assert abs(result.interaction_effect - 0.05) <= ATOL
    _assert_identity(result)


def test_single_asset_universe_has_zero_allocation_and_pure_selection():
    pw = {"ONLY": 0.8}  # 20% cash
    bw = {"ONLY": 1.0}
    rets = {"ONLY": 0.07}
    sector_map = {"ONLY": "solo"}

    result = brinson_attribution(pw, bw, rets, sector_map)

    row = result.sector("solo")
    assert abs(row.allocation_effect - (0.8 - 1.0) * 0.07) <= ATOL
    assert abs(row.selection_effect - 1.0 * (0.07 - 0.07)) <= ATOL
    assert abs(row.interaction_effect - (0.8 - 1.0) * 0.0) <= ATOL
    assert abs(result.active_return - (0.8 - 1.0) * 0.07) <= ATOL
    _assert_identity(result)


def test_one_sided_sector_buckets_are_finite_and_exact():
    """Benchmark-only and portfolio-only sectors: no NaN, interaction carries them."""
    pw = {"AAA": 0.60, "BBB": 0.40, "CCC": 0.00}
    bw = {"AAA": 0.20, "BBB": 0.00, "CCC": 0.80}
    rets = {"AAA": 0.04, "BBB": 0.09, "CCC": -0.01}
    sector_map = {"AAA": "core", "BBB": "portfolio_only", "CCC": "benchmark_only"}

    result = brinson_attribution(pw, bw, rets, sector_map)

    port_only = result.sector("portfolio_only")   # w_b == 0
    bench_only = result.sector("benchmark_only")  # w_p == 0

    assert port_only.benchmark_weight == 0.0
    assert port_only.benchmark_return == 0.0  # reported, not NaN
    assert port_only.allocation_effect == 0.0
    assert port_only.selection_effect == 0.0
    assert abs(port_only.interaction_effect - 0.40 * 0.09) <= ATOL

    assert bench_only.portfolio_weight == 0.0
    assert bench_only.portfolio_return == 0.0
    assert abs(bench_only.interaction_effect - ((0.0 - 0.80) * (0.0 - (-0.01)))) <= ATOL

    for row in result.sectors:
        effects = [row.allocation_effect, row.selection_effect, row.interaction_effect]
        assert np.isfinite(effects).all()
    _assert_identity(result)


def test_nan_return_is_disclosed_not_dropped_and_identity_survives():
    pw = {"AAA": 0.5, "BBB": 0.5}
    bw = {"AAA": 0.5, "BBB": 0.5}
    rets = {"AAA": 0.06, "BBB": float("nan")}
    sector_map = {"AAA": "tech", "BBB": "energy"}

    result = brinson_attribution(pw, bw, rets, sector_map)

    assert result.nan_return_symbols == ("BBB",)
    # The symbol keeps its weight in the report (0.5 + 0.5 == 1.0), it is not dropped.
    assert abs(sum(r.portfolio_weight for r in result.sectors) - 1.0) <= ATOL
    assert result.sector("energy").portfolio_return == 0.0
    assert abs(result.portfolio_return - 0.03) <= ATOL
    _assert_identity(result)


def test_nan_weights_are_disclosed_and_do_not_break_the_identity():
    pw = {"AAA": float("nan"), "BBB": 1.0}
    bw = {"AAA": 0.5, "BBB": 0.5}
    rets = {"AAA": 0.10, "BBB": 0.02}

    result = brinson_attribution(pw, bw, rets, {"AAA": "a", "BBB": "b"})

    assert result.nan_weight_symbols == ("AAA",)
    assert abs(result.portfolio_return - 0.02) <= ATOL
    _assert_identity(result)


def test_empty_inputs_return_zeroed_attribution():
    result = brinson_attribution({}, {}, {}, {})

    assert result.sectors == ()
    assert result.portfolio_return == 0.0
    assert result.benchmark_return == 0.0
    assert result.active_return == 0.0
    assert result.residual == 0.0
    assert result.to_dataframe().empty


# --------------------------------------------------------------------------- #
# 8. Input validation
# --------------------------------------------------------------------------- #
def test_invalid_fallback_is_rejected_even_on_an_empty_universe():
    with pytest.raises(ValueError):
        brinson_attribution({}, {}, {}, {}, fallback="unclassified")


def test_duplicate_symbol_index_is_rejected():
    import pandas as pd

    broken = pd.Series([0.5, 0.5], index=["AAA", "AAA"])
    with pytest.raises(ValueError):
        brinson_attribution(broken, {"AAA": 1.0}, {"AAA": 0.01})


def test_weights_are_not_renormalised():
    """A 50%-cash portfolio must not be silently scaled up to 100%."""
    result = brinson_attribution({"AAA": 0.5}, {"AAA": 1.0}, {"AAA": 0.10}, {"AAA": "a"})
    assert abs(result.portfolio_return - 0.05) <= ATOL
    assert abs(result.active_return - (-0.05)) <= ATOL
    _assert_identity(result)


def test_sector_contribution_row_is_a_plain_frozen_dataclass():
    from dataclasses import FrozenInstanceError

    row = SectorContribution(
        sector="x",
        portfolio_weight=0.5,
        benchmark_weight=0.25,
        portfolio_return=0.02,
        benchmark_return=0.01,
        allocation_effect=-0.0025,
        selection_effect=0.0025,
        interaction_effect=0.0025,
    )
    assert abs(row.total_effect - 0.0025) <= ATOL
    assert row.as_dict()["sector"] == "x"
    with pytest.raises(FrozenInstanceError):
        row.sector = "y"


"""Offline regression suite for `R1` -- intraday drawdown (`hwm_intra` / `dd_trough`) => exposure downshift.

Unit under test: `common.intraday_drawdown_derisk` (read its module docstring and the frozen
judgement `docs/intraday_drawdown_derisk.md`, not this file, if the two ever disagree).

WHAT IS PINNED (and what must turn red)
---------------------------------------
1. Each band boundary belongs to the **deeper** band (`<=` de-risks): `dd = -0.02` => `0.75`,
   `-0.03` => `0.50`, `-0.04` => `0.25`; one ULP above each boundary moves to the lighter band.
2. The no-op path returns the **literal** `1.0` (bit-identical; `repr == "1.0"`), including
   `dd == 0.0`, which is a *measurement* ("sitting exactly at the high"), not missing data.
3. Tri-state: missing / `NaN` / zero / negative `hwm_intra`, missing / `NaN` / non-positive price,
   non-finite `dd_trough`, a positive `dd_trough`, and an anchor inconsistent with the reference
   price all give `INSUFFICIENT_DATA` + multiplier `1.0` + `drawdown is None` -- **never `0.0`**.
4. The ladder is monotonic: a deeper drawdown never yields a higher multiplier.
5. The ladder is stateless: the same call twice returns equal decisions, interleaved calls do not
   perturb each other, and no module-level mutable state exists.
6. The round-1 advisory fence travels on every state, and the dataclass **refuses** to be built as
   an order gate (`advisory=False` / `gates_orders=True` raise).
7. AST: this module never imports / calls `R4` (`common.cs_dispersion_exposure`).
8. AST: no `R4`-style **ratio** band constant was copied in -- every band threshold is a strictly
   negative *drawdown* fraction, and the multiplier domain (0, 1) is separated from the threshold
   domain. Honest limit, disclosed: a static scan can prove "no positive ratio constants and no
   `R4` constant names", it cannot prove "no threshold was numerically inspired by `R4`"; the
   discriminating property (sign) is what is asserted here.

100% offline: scalar constructions plus one panel from `common.intraday_synthetic`. No network, no
cache, no real market data, no `data/runs/**`, no `backtests_research/`, no production report file.
"""

from __future__ import annotations

import ast
import dataclasses
import math
import os
import sys
import types

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import common.intraday_drawdown_derisk as idd  # noqa: E402
from common.intraday_drawdown_derisk import (  # noqa: E402
    DEFAULT_DEEP_DRAWDOWN_THRESH,
    DEFAULT_LIGHT_DRAWDOWN_THRESH,
    DEFAULT_MEDIUM_DRAWDOWN_THRESH,
    MULTIPLIER_DEEP,
    MULTIPLIER_LIGHT,
    MULTIPLIER_MEDIUM,
    MULTIPLIER_UNCHANGED,
    REFERENCE_PRICE_SEMANTICS,
    STATUSES,
    IntradayDrawdownConfig,
    IntradayDrawdownDecision,
    IntradayDrawdownStatus,
    anchor_hwm_intra,
    classify_intraday_drawdown,
    compute_intraday_drawdown,
    evaluate_intraday_drawdown,
)

_MODULE_PATH = os.path.join(_REPO_ROOT, "common", "intraday_drawdown_derisk.py")

#: The frozen ladder, as recorded in `docs/intraday_drawdown_derisk.md` §2 -- written out
#: independently of the module so a silent constant edit cannot agree with itself in the tests.
FROZEN_LADDER = (
    (0.0, MULTIPLIER_UNCHANGED, "NOT_TRIGGERED"),
    (-0.0199, MULTIPLIER_UNCHANGED, "NOT_TRIGGERED"),
    (-0.02, MULTIPLIER_LIGHT, "DERISK_LIGHT"),
    (-0.025, MULTIPLIER_LIGHT, "DERISK_LIGHT"),
    (-0.03, MULTIPLIER_MEDIUM, "DERISK_MEDIUM"),
    (-0.035, MULTIPLIER_MEDIUM, "DERISK_MEDIUM"),
    (-0.04, MULTIPLIER_DEEP, "DERISK_DEEP"),
    (-0.075, MULTIPLIER_DEEP, "DERISK_DEEP"),
)


def _decisions(**kwargs) -> IntradayDrawdownDecision:
    return evaluate_intraday_drawdown(**kwargs)


# --------------------------------------------------------------------------- #
# 1. band boundaries
# --------------------------------------------------------------------------- #
def test_band_boundaries_belong_to_the_deeper_band():
    """`<=` de-risks: equality lands in the deeper band, at each of the three boundaries."""
    # The threshold constants are already negative fractions (`-0.02`), i.e. drawdowns.
    assert DEFAULT_DEEP_DRAWDOWN_THRESH < DEFAULT_MEDIUM_DRAWDOWN_THRESH < DEFAULT_LIGHT_DRAWDOWN_THRESH < 0
    for drawdown, multiplier, status in (
        (DEFAULT_LIGHT_DRAWDOWN_THRESH, MULTIPLIER_LIGHT, "DERISK_LIGHT"),
        (DEFAULT_MEDIUM_DRAWDOWN_THRESH, MULTIPLIER_MEDIUM, "DERISK_MEDIUM"),
        (DEFAULT_DEEP_DRAWDOWN_THRESH, MULTIPLIER_DEEP, "DERISK_DEEP"),
        (-0.02, MULTIPLIER_LIGHT, "DERISK_LIGHT"),
        (-0.03, MULTIPLIER_MEDIUM, "DERISK_MEDIUM"),
        (-0.04, MULTIPLIER_DEEP, "DERISK_DEEP"),
    ):
        # Pinned twice on purpose: through the pure classifier (exact floats, no arithmetic) and
        # through the public evaluator (the path a caller actually walks).
        assert classify_intraday_drawdown(drawdown)[:2] == (status, multiplier)
        decision = _decisions(hwm_intra=100.0, dd_trough=drawdown)
        assert decision.status == status
        assert decision.multiplier == multiplier
        assert decision.drawdown == drawdown

    # And the price path agrees with the frozen definition (`price / hwm - 1`), not a re-derivation.
    assert _decisions(hwm_intra=100.0, price=98.0).drawdown == pytest.approx(-0.02, abs=1e-15)
    assert _decisions(hwm_intra=100.0, price=98.0).multiplier == MULTIPLIER_LIGHT
    assert _decisions(hwm_intra=100.0, price=97.0).status == "DERISK_MEDIUM"
    assert _decisions(hwm_intra=100.0, price=96.0).status == "DERISK_DEEP"


def test_one_ulp_above_each_boundary_moves_to_the_lighter_band():
    """One ULP *above* a boundary is the lighter band -- the bands are not "sticky inward"."""
    for boundary, expected_multiplier, expected_status in (
        (DEFAULT_LIGHT_DRAWDOWN_THRESH, MULTIPLIER_UNCHANGED, "NOT_TRIGGERED"),
        (DEFAULT_MEDIUM_DRAWDOWN_THRESH, MULTIPLIER_LIGHT, "DERISK_LIGHT"),
        (DEFAULT_DEEP_DRAWDOWN_THRESH, MULTIPLIER_MEDIUM, "DERISK_MEDIUM"),
    ):
        just_above = math.nextafter(boundary, 0.0)
        assert just_above > boundary, "nextafter must move towards zero (a shallower drawdown)"
        status, multiplier, _ = classify_intraday_drawdown(just_above)
        assert (status, multiplier) == (expected_status, expected_multiplier)
        decision = _decisions(hwm_intra=100.0, dd_trough=just_above)
        assert (decision.status, decision.multiplier) == (expected_status, expected_multiplier)

    # The boundary itself still de-risks, so the two assertions together pin the direction.
    status, multiplier, _ = classify_intraday_drawdown(DEFAULT_LIGHT_DRAWDOWN_THRESH)
    assert (status, multiplier) == ("DERISK_LIGHT", MULTIPLIER_LIGHT)


# --------------------------------------------------------------------------- #
# 2. no-op path
# --------------------------------------------------------------------------- #
def test_no_op_path_returns_exactly_one():
    """`dd > -2.0%` is the literal `1.0` -- and `dd == 0.0` is a reading, not missing data."""
    for shallow in (0.0, -1e-12, -0.005, -0.019999, -0.01):
        decision = _decisions(hwm_intra=250_000.0, dd_trough=shallow)
        assert decision.multiplier == MULTIPLIER_UNCHANGED
        assert repr(decision.multiplier) == "1.0", "must be the literal 1.0, not 0.9999999"
        assert math.copysign(1.0, decision.multiplier) == 1.0
        assert decision.status == "NOT_TRIGGERED"
        assert decision.drawdown == shallow
        assert decision.drawdown is not None, "a measured shallow drawdown is never None"

    # Exactly at the high: measured, unchanged, and NOT `INSUFFICIENT_DATA`.
    at_high = _decisions(hwm_intra=100.0, price=100.0)
    assert (at_high.status, at_high.multiplier, at_high.drawdown) == ("NOT_TRIGGERED", 1.0, 0.0)

    # The production anchor also yields a bit-identical no-op when the session never dipped.
    hwm = anchor_hwm_intra(99_000.0, [99_000.0, 99_500.0, 99_120.0])
    assert hwm == 99_500.0
    assert _decisions(hwm_intra=hwm, price=hwm).multiplier == 1.0


# --------------------------------------------------------------------------- #
# 3. tri-state
# --------------------------------------------------------------------------- #
def test_missing_or_degenerate_inputs_are_insufficient_not_zero():
    """Every unmeasurable input is `INSUFFICIENT_DATA` + `1.0` + `drawdown is None` (never 0.0)."""
    cases = [
        {"hwm_intra": None, "price": 100.0},
        {"hwm_intra": float("nan"), "price": 100.0},
        {"hwm_intra": float("inf"), "price": 100.0},
        {"hwm_intra": 0.0, "price": 100.0},
        {"hwm_intra": -5.0, "price": 100.0},
        {"hwm_intra": "not-a-number", "price": 100.0},
        {"hwm_intra": True, "price": 100.0},
        {"hwm_intra": 100.0, "price": None},
        {"hwm_intra": 100.0, "price": float("nan")},
        {"hwm_intra": 100.0, "price": 0.0},
        {"hwm_intra": 100.0, "price": -3.0},
        {"hwm_intra": 100.0, "dd_trough": None},
        {"hwm_intra": 100.0, "dd_trough": float("nan")},
        {"hwm_intra": 100.0, "dd_trough": float("-inf")},
        {"hwm_intra": 100.0, "dd_trough": "oops"},
        {"hwm_intra": 100.0},  # neither price nor dd_trough: no trough reading at all
        {"hwm_intra": 0.0, "dd_trough": 0.0},  # the producer's `else 0.0` degradation
        {"hwm_intra": 100.0, "dd_trough": -0.05, "reference_price": 120.0},  # anchor inconsistent
    ]
    for kwargs in cases:
        decision = _decisions(**kwargs)
        assert decision.status == IntradayDrawdownStatus.INSUFFICIENT_DATA.value, kwargs
        assert decision.multiplier == 1.0, kwargs
        assert repr(decision.multiplier) == "1.0", kwargs
        assert decision.drawdown is None, f"insufficient data must be None, never 0.0: {kwargs}"
        assert decision.dd_source == "none", kwargs
        assert decision.reason.startswith("INSUFFICIENT_DATA"), kwargs
        # The fence is present in the degraded state too.
        assert decision.advisory is True and decision.gates_orders is False, kwargs

    # `INSUFFICIENT_DATA` != `NOT_TRIGGERED`: same multiplier, different status.
    insufficient = _decisions(hwm_intra=None, price=100.0)
    not_triggered = _decisions(hwm_intra=100.0, price=100.0)
    assert insufficient.multiplier == not_triggered.multiplier == 1.0
    assert insufficient.status != not_triggered.status
    assert insufficient.drawdown is None and not_triggered.drawdown == 0.0

    # The invariant is structural: a hand-built "insufficient 0.0" cannot exist.
    with pytest.raises(ValueError):
        IntradayDrawdownDecision(
            multiplier=1.0, drawdown=0.0, status="INSUFFICIENT_DATA", reason="hand-built"
        )
    with pytest.raises(ValueError):
        IntradayDrawdownDecision(
            multiplier=0.5, drawdown=None, status="INSUFFICIENT_DATA", reason="hand-built"
        )


# --------------------------------------------------------------------------- #
# 4. monotonic ladder
# --------------------------------------------------------------------------- #
def test_ladder_is_monotonic():
    """A deeper drawdown never yields a *higher* multiplier, and never leaves the frozen table."""
    ladder = [i / 1000.0 for i in range(0, 101)]  # 0.0 .. -0.10 in 0.1% steps
    previous = None
    for step in ladder:
        drawdown = -step
        status, multiplier, _ = classify_intraday_drawdown(drawdown)
        # `FROZEN_LADDER` lists shallower bounds first, so the *last* matching boundary is the
        # deepest one that applies -- recomputed here from the table, not from the module.
        matches = [(bound, m) for bound, m, _ in FROZEN_LADDER if drawdown <= bound and bound < 0.0]
        expected = matches[-1][1] if matches else MULTIPLIER_UNCHANGED
        assert multiplier == expected, (drawdown, multiplier, expected)
        if previous is not None:
            assert multiplier <= previous, f"multiplier rose as drawdown deepened at {drawdown}"
        previous = multiplier

    # And the multiplier set is exactly the frozen one -- no accidental fourth value.
    used = {classify_intraday_drawdown(-step)[1] for step in ladder}
    assert used == {MULTIPLIER_UNCHANGED, MULTIPLIER_LIGHT, MULTIPLIER_MEDIUM, MULTIPLIER_DEEP}
    assert 0.0 not in used, "the floor is 0.25: R1 is advisory and never liquidates"


# --------------------------------------------------------------------------- #
# 5. stateless reset
# --------------------------------------------------------------------------- #
def test_ladder_is_stateless():
    """Same call => equal decision; interleaving other calls changes nothing; no hidden state."""
    deep = dict(hwm_intra=100.0, dd_trough=-0.045)
    shallow = dict(hwm_intra=100.0, dd_trough=-0.005)

    first = _decisions(**deep)
    second = _decisions(**deep)
    assert first == second
    assert (first.status, first.multiplier) == ("DERISK_DEEP", MULTIPLIER_DEEP)

    # Reset is immediate: no hysteresis, no cooldown, no "was de-risked last time" memory.
    _decisions(**shallow)
    assert _decisions(**deep) == first
    assert _decisions(**shallow).multiplier == MULTIPLIER_UNCHANGED

    # Interleaved order can never matter.
    sequence = [_decisions(**deep), _decisions(**shallow), _decisions(**deep)]
    assert sequence[0] == sequence[2]

    # No module-level mutable container that a caller could poison between calls.
    for name, value in vars(idd).items():
        if name.startswith("__") or name == "annotations":
            # `annotations` is `from __future__ import annotations` (a `_Feature`, not data).
            continue
        if isinstance(value, type) or callable(value) or isinstance(value, types.ModuleType):
            continue
        assert isinstance(value, (str, int, float, tuple, frozenset)), (
            f"module-level {name!r} is a mutable/global-state candidate: {type(value).__name__}"
        )

    # The config itself is frozen as well.
    cfg = IntradayDrawdownConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.light_multiplier = 1.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 6. advisory fence
# --------------------------------------------------------------------------- #
def test_advisory_fence_in_every_state():
    """`advisory=True` / `gates_orders=False` on every status, and the fence is enforced."""
    observed = set()
    for drawdown in (0.0, -0.025, -0.035, -0.05, None):
        decision = _decisions(hwm_intra=100.0, dd_trough=drawdown)
        observed.add(decision.status)
        assert decision.advisory is True
        assert decision.gates_orders is False
    assert observed == set(STATUSES), "every status must carry the fence"

    # A caller cannot promote a result into an order gate, and this module exposes no applier.
    with pytest.raises(ValueError):
        IntradayDrawdownDecision(
            multiplier=0.5, drawdown=-0.03, status="DERISK_MEDIUM", reason="hand-built",
            gates_orders=True,
        )
    with pytest.raises(ValueError):
        IntradayDrawdownDecision(
            multiplier=0.5, drawdown=-0.03, status="DERISK_MEDIUM", reason="hand-built",
            advisory=False,
        )
    assert not [n for n in dir(idd) if n.startswith("apply_")], (
        "round 1 ships no application helper: the only thing a caller may do is print the result"
    )


# --------------------------------------------------------------------------- #
# 7. R4 isolation (AST)
# --------------------------------------------------------------------------- #
def _module_tree() -> ast.Module:
    with open(_MODULE_PATH, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=_MODULE_PATH)


def test_module_never_imports_or_calls_r4():
    """Static proof of the `R1` != `R4` fence: no import, no name, no call into the sibling rule."""
    tree = _module_tree()

    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported, "the module imports something; the scan must not be vacuous"
    offenders = [name for name in imported if "cs_dispersion" in name]
    assert not offenders, f"R1 must not import R4's module, found: {offenders}"

    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
    } | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = ("dispersion", "baseline", "collapse", "cs_dispersion")
    hits = sorted(
        name for name in identifiers if any(word in name.lower() for word in forbidden)
    )
    assert not hits, f"R1 must not touch R4's vocabulary, found identifiers: {hits}"

    # Nothing R4-shaped leaked into the module namespace either (e.g. a re-exported helper).
    leaked = [
        name for name in dir(idd)
        if any(word in name.lower() for word in forbidden)
    ]
    assert not leaked, f"R4 symbols leaked into R1's namespace: {leaked}"


# --------------------------------------------------------------------------- #
# 8. no R4 ratio-band constants (AST)
# --------------------------------------------------------------------------- #
def test_no_r4_ratio_band_constants_are_copied():
    """Threshold constants are strictly negative drawdown fractions; R4's ratio names are absent."""
    tree = _module_tree()

    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                constants[name] = float(value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                constants[node.target.id] = float(value)

    assert constants, "no numeric module constants were found; the scan must not be vacuous"

    thresholds = {n: v for n, v in constants.items() if n.endswith("_THRESH")}
    assert set(thresholds) == {
        "DEFAULT_LIGHT_DRAWDOWN_THRESH",
        "DEFAULT_MEDIUM_DRAWDOWN_THRESH",
        "DEFAULT_DEEP_DRAWDOWN_THRESH",
    }
    assert set(thresholds.values()) == {
        DEFAULT_LIGHT_DRAWDOWN_THRESH,
        DEFAULT_MEDIUM_DRAWDOWN_THRESH,
        DEFAULT_DEEP_DRAWDOWN_THRESH,
    } == {-0.02, -0.03, -0.04}
    for name, value in thresholds.items():
        assert value < 0.0, f"{name} is a drawdown threshold and must be negative, got {value!r}"
        assert value <= -0.02, f"{name} must sit at or beyond -2%, got {value!r}"

    multipliers = {n: v for n, v in constants.items() if n.startswith("MULTIPLIER_")}
    assert set(multipliers.values()) == {
        MULTIPLIER_UNCHANGED, MULTIPLIER_LIGHT, MULTIPLIER_MEDIUM, MULTIPLIER_DEEP
    } == {1.0, 0.75, 0.5, 0.25}
    for name, value in multipliers.items():
        assert 0.0 < value <= 1.0, f"{name} must stay in (0, 1], got {value!r}"

    # R4's band/threshold constant names must not appear here. (Multiplier names are shared
    # vocabulary -- `0.75` as a *multiplier* exists in both rules and is not a copied threshold.)
    r4_names = {
        "LIGHT_COLLAPSE_RATIO", "FULL_COLLAPSE_RATIO", "RETURN_LOOKBACK_BARS",
        "DEFAULT_BASELINE_WINDOW", "MIN_CROSS_SECTION_SYMBOLS", "DEFAULT_BAR_LABEL",
        "BARS_PER_SESSION", "CASH_PROXY_SYMBOLS",
    }
    assert not (set(constants) & r4_names), (
        f"R4 constants copied into R1: {sorted(set(constants) & r4_names)}"
    )
    assert not [n for n in constants if "RATIO" in n.upper()], (
        "R4's bands are `D/B` *ratios*; R1's bands are absolute drawdown fractions and must not "
        f"be named as ratios: {sorted(n for n in constants if 'RATIO' in n.upper())}"
    )

    # No comparison may be built against a dispersion/baseline operand.
    compared_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                for inner in ast.walk(operand):
                    if isinstance(inner, ast.Name):
                        compared_names.add(inner.id)
    assert not [
        n for n in compared_names
        if any(w in n.lower() for w in ("dispersion", "baseline", "ratio", "collapse"))
    ], f"an R4-style ratio comparison survived: {sorted(compared_names)}"


# --------------------------------------------------------------------------- #
# extra: input plumbing, domain guards, anchor, config
# --------------------------------------------------------------------------- #
def test_price_and_dd_trough_are_mutually_exclusive():
    """Two readings of the same quantity must not be silently reconciled."""
    with pytest.raises(ValueError):
        evaluate_intraday_drawdown(100.0, price=98.0, dd_trough=-0.02)


def test_positive_drawdown_is_out_of_domain():
    """`dd > 0` means the HWM is not a peak: refused, never a silent clean no-op."""
    assert classify_intraday_drawdown(0.0005)[:2] == ("INSUFFICIENT_DATA", 1.0)
    decision = _decisions(hwm_intra=100.0, price=105.0)
    assert decision.status == "INSUFFICIENT_DATA"
    assert decision.drawdown is None
    assert decision.multiplier == 1.0
    assert compute_intraday_drawdown(100.0, 105.0) is None
    assert compute_intraday_drawdown(0.0, 100.0) is None
    assert compute_intraday_drawdown(100.0, 98.0) == pytest.approx(-0.02)


def test_anchor_mirrors_the_producer():
    """`anchor_hwm_intra` == `max(prior peak, session peak)`, the producer's `:2856` expression."""
    assert anchor_hwm_intra(100.0, [99.0, 103.0, 101.0]) == 103.0
    assert anchor_hwm_intra(105.0, [99.0, 100.0]) == 105.0
    assert anchor_hwm_intra(None, []) is None
    assert anchor_hwm_intra(0.0, []) is None
    # Non-positive / non-finite prices are excluded, never read as a 0.0 price.
    assert anchor_hwm_intra(100.0, [float("nan"), -1.0, 0.0]) == 100.0
    assert anchor_hwm_intra(None, [float("inf")]) is None

    # End to end: the anchored HWM reproduces the frozen drawdown definition exactly.
    prices = [100.0, 99.5, 97.0, 96.0, 98.5]
    hwm = anchor_hwm_intra(100.0, prices)
    decision = _decisions(hwm_intra=hwm, price=min(prices))
    assert decision.drawdown == pytest.approx(min(prices) / hwm - 1.0)
    # 96 from a peak of 100 is exactly -4.0%, i.e. the deepest band (equality de-risks).
    assert decision.drawdown == pytest.approx(-0.04)
    assert decision.status == "DERISK_DEEP"
    assert (decision.status, decision.multiplier) == classify_intraday_drawdown(decision.drawdown)[:2]
    assert decision.reference_semantics == REFERENCE_PRICE_SEMANTICS == "previous_close"

    # A path that never dips below -2.0% stays a bit-identical no-op.
    shallow = _decisions(hwm_intra=anchor_hwm_intra(100.0, [99.0, 99.8]), price=99.0)
    assert (shallow.status, shallow.multiplier) == ("NOT_TRIGGERED", 1.0)


def test_config_refuses_inverted_or_positive_bands():
    """A positive or inverted band is a dead / wrong-domain threshold, refused at construction."""
    with pytest.raises(ValueError):
        IntradayDrawdownConfig(light_drawdown_thresh=0.02)
    with pytest.raises(ValueError):
        # Inverted: the medium band is deeper than the deep band, so the deep band is unreachable.
        IntradayDrawdownConfig(medium_drawdown_thresh=-0.05)
    with pytest.raises(ValueError):
        IntradayDrawdownConfig(light_drawdown_thresh=-0.03, medium_drawdown_thresh=-0.03)
    with pytest.raises(ValueError):
        IntradayDrawdownConfig(deep_multiplier=0.0)
    with pytest.raises(ValueError):
        IntradayDrawdownConfig(light_multiplier=1.0)
    with pytest.raises(ValueError):
        IntradayDrawdownConfig(deep_multiplier=0.60, medium_multiplier=0.50)
    with pytest.raises(ValueError):
        IntradayDrawdownConfig(light_drawdown_thresh=float("nan"))


def test_evaluate_on_a_synthetic_intraday_path():
    """A `common.intraday_synthetic` panel drives the whole judgement 100% offline."""
    from common.intraday_synthetic import build_synthetic_5m_panel

    panel = build_synthetic_5m_panel(symbols=2, sessions=2, seed=7)
    bars = panel.bars
    symbol = sorted(bars["symbol"].unique())[0]
    close = (
        bars.loc[bars["symbol"] == symbol]
        .sort_values("ts")["close"]
        .astype(float)
        .to_numpy()
    )
    assert close.size and min(close) > 0.0

    previous_close = float(close[0])
    hwm = anchor_hwm_intra(previous_close, close)
    trough = float(close.min())
    decision = _decisions(hwm_intra=hwm, price=trough, reference_price=previous_close)

    assert decision.status in STATUSES
    assert decision.drawdown == pytest.approx(trough / hwm - 1.0)
    assert (decision.status, decision.multiplier) == classify_intraday_drawdown(decision.drawdown)[:2]
    assert decision.advisory is True and decision.gates_orders is False
    # Feeding the same reading as `dd_trough` yields the identical verdict (one quantity, one rule).
    via_field = _decisions(hwm_intra=hwm, dd_trough=decision.drawdown,
                           reference_price=previous_close)
    assert (via_field.status, via_field.multiplier, via_field.drawdown) == (
        decision.status, decision.multiplier, decision.drawdown
    )

"""Offline regression suite for `common/live_capacity.py` (development_plan `AT4`).

`AT4` (docs/development_plan.md line 410) asks for live-side observation only of
(a) turnover inflation against the `L2.36` pre-registered lines and (b) volume
participation on BOTH bases (占当日成交量 / 占近20日均量), with missing volume
falling to an explicit tri-state -- never `0` / mean fill -- and everything
advisory (no gating of any order).

What these tests pin, in order:

1. the two constants are literally the `L2.36` pre-registered values, read back
   out of `docs/development_plan_archive.md:261` (mirror test, not a re-typed
   number), and the module contains NO other turnover-threshold literal (AST
   scan of every float constant in the file);
2. annualisation equals a hand-computed value, and the two-way / one-way basis
   distinction matches `common/allocation_backtester.py`'s `total_turnover`
   definition (source-pinned: both legs add to `notional`, then
   `notional / exec_equity` is accumulated);
3. both participation bases on a hand-checkable example;
4. every missing / zero / NaN / short-history volume input is tri-state with
   `None` numerics -- the test asserts the values are NOT `0.0` and NOT the
   mean;
5. labels are advisory: no exceptions on extreme / garbage input, no side
   effects, no writes, boundaries strict (`>`), and the report is flat JSON-safe.

100% offline: hand-written numbers only. No network, no real market data, no
file writes (the doc read is a read-only provenance check on a repo file).
"""

import ast
import json
import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common import live_capacity as lc
from common.live_capacity import (
    TURNOVER_CEILING_ANN_ONE_WAY,
    TURNOVER_QUALIFIED_ANN_ONE_WAY,
    annualized_turnover,
    capacity_labels,
    turnover_label,
    volume_participation,
)

_MODULE_PATH = os.path.join(_REPO_ROOT, "common", "live_capacity.py")
_DOC_PATH = os.path.join(_REPO_ROOT, "docs", "development_plan_archive.md")


def _module_source() -> str:
    with open(_MODULE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _module_float_constants() -> list:
    """Every float literal in the module, wherever it appears."""
    tree = ast.parse(_module_source())
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)]


# ---------------------------------------------------------------------------
# 1. Threshold provenance -- L2.36 only
# ---------------------------------------------------------------------------
def test_turnover_constants_mirror_l2_36_and_no_other_threshold_exists():
    # (a) the numbers still exist in the L2.36 row, with the same wording
    with open(_DOC_PATH, encoding="utf-8") as fh:
        row = fh.read().splitlines()[lc.L2_36_ARCHIVE_LINE - 1]
    assert "`L2.36`" in row, "archive line drifted; L2_36_ARCHIVE_LINE must be re-pinned"
    match = re.search(r"合格 \*\*≤ ([\d.]+)×/年\*\*、硬上限 \*\*≤ ([\d.]+)×/年\*\*", row)
    assert match is not None, "L2.36 预注册阈值 wording changed -- re-read the doc before touching constants"
    assert "单边" in row and "预注册阈值" in row
    doc_qualified, doc_ceiling = float(match.group(1)), float(match.group(2))

    # (b) the module's constants ARE those numbers (no re-typed drift)
    assert TURNOVER_QUALIFIED_ANN_ONE_WAY == doc_qualified == 8.0
    assert TURNOVER_CEILING_ANN_ONE_WAY == doc_ceiling == 10.0

    # (c) the module cites the exact source line, including the one-way basis
    assert "development_plan_archive.md:261" in lc.L2_36_SOURCE
    assert "L2.36" in lc.L2_36_SOURCE
    assert "单边" in lc.L2_36_SOURCE

    # (d) NO other turnover-threshold literal anywhere in the file. Any float in
    #     the (2.5, 250.0) band would be a plausible candidate for a silently
    #     invented second line ("just bump it to 12"), so the band must contain
    #     exactly the two pre-registered values. 252.0 (periods/year) sits
    #     outside it by design; 0.0 / 1.0 / 2.0 are structural, not thresholds.
    suspicious = sorted({v for v in _module_float_constants() if 2.5 < v < 250.0})
    assert suspicious == [8.0, 10.0], (
        f"unexpected turnover-threshold literal(s) {suspicious}; AT4 clause 2 forbids "
        "any threshold other than the L2.36 8.0 / 10.0 pair"
    )

    # (e) and no threshold-sounding module-level NAME exists beyond those two
    tree = ast.parse(_module_source())
    named = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        named.update(targets)
    thresholdish = {
        name for name in named
        if "TURNOVER" in name.upper()
        and any(tag in name.upper() for tag in ("CEIL", "QUALIF", "THRESH", "LIMIT", "CAP"))
    }
    assert thresholdish == {
        "TURNOVER_QUALIFIED_ANN_ONE_WAY",
        "TURNOVER_CEILING_ANN_ONE_WAY",
    }, f"only the two L2.36 constants may look like turnover thresholds, got {sorted(thresholdish)}"


# ---------------------------------------------------------------------------
# 2. Annualisation against a hand-computed value + basis convention
# ---------------------------------------------------------------------------
def test_annualized_turnover_matches_hand_computed_values():
    # Window of 4 periods, one 1000 notional print in period 0, flat 10,000
    # equity: sum(n/e) = 0.1; x (252/4) = 6.3 two-way.
    obs = annualized_turnover([1000.0, 0.0, 0.0, 0.0], 10000.0)
    assert obs.available is True
    assert obs.notional_over_equity_sum == pytest.approx(0.1, rel=1e-15)
    assert obs.annualized == pytest.approx(6.3, rel=1e-12)
    assert obs.one_way_annualized == pytest.approx(3.15, rel=1e-12)
    assert obs.window_periods == 4 and obs.n_periods == 4
    assert obs.traded_notional_total == pytest.approx(1000.0)

    # Per-period equity: (500/10000) + (1500/20000) = 0.05 + 0.075 = 0.125;
    # x (252/2) = 15.75 two-way / 7.875 one-way.
    varied = annualized_turnover([500.0, 1500.0], [10000.0, 20000.0])
    assert varied.annualized == pytest.approx(15.75, rel=1e-12)
    assert varied.one_way_annualized == pytest.approx(7.875, rel=1e-12)

    # Sparse rebalance rows + explicit window length (2 rebalances over 63 days,
    # the L2.36 fold geometry) annualises by 252/63 = 4.0.
    sparse = annualized_turnover([12_000.0, 8_000.0], 100_000.0, window_periods=63)
    assert sparse.annualized == pytest.approx(20_000.0 / 100_000.0 * 4.0, rel=1e-12)
    assert sparse.n_periods == 2 and sparse.window_periods == 63

    # Scalar-equity broadcast equals an equal-length list of that scalar.
    broadcast = annualized_turnover([300.0, 300.0, 300.0], 5000.0, periods_per_year=252.0)
    listed = annualized_turnover([300.0, 300.0, 300.0], [5000.0] * 3, periods_per_year=252.0)
    assert broadcast.annualized == pytest.approx(listed.annualized, rel=1e-15)


def test_two_way_convention_matches_backtester_total_turnover_definition():
    """`L2.36`'s lines are 单边; the backtester's `total_turnover` is 双边.

    Pinned at the source so the basis cannot silently drift: `notional` receives
    `trade_val` on BOTH legs (SELL and BUY), then is divided by `exec_equity`.
    A one-way series is therefore half of a two-way series, never the same
    number -- the exact 2x error `L2.49` item 7 had to correct.
    """
    with open(os.path.join(_REPO_ROOT, "common", "allocation_backtester.py"), encoding="utf-8") as fh:
        bt_source = fh.read()
    assert "total_turnover += notional / exec_equity" in bt_source
    # Two accumulating legs (SELL + BUY) in each of the two execution engines.
    # Matched as stripped lines so `rebal_notional += trade_val` cannot be
    # counted as a `notional` leg by substring accident.
    stripped = [line.strip() for line in bt_source.splitlines()]
    assert stripped.count("notional += trade_val") == 2
    assert stripped.count("rebal_notional += trade_val") == 2

    two_way = annualized_turnover([4000.0], 100_000.0)
    one_way = annualized_turnover([2000.0], 100_000.0, convention="one_way")
    # Same economic round trip, two ways of stating it: a 4,000 two-way print
    # (2,000 bought + 2,000 sold) is a 2,000 one-way print.
    assert two_way.one_way_annualized == pytest.approx(one_way.one_way_annualized, rel=1e-12)
    assert two_way.annualized == pytest.approx(one_way.annualized * 2.0, rel=1e-12)
    assert two_way.one_way_annualized == pytest.approx(two_way.annualized / 2.0, rel=1e-12)
    assert two_way.convention == "two_way" and one_way.convention == "one_way"

    # An unknown basis is a caller bug, never a silent default.
    with pytest.raises(ValueError):
        annualized_turnover([1.0], 100.0, convention="gross")


def test_annualized_turnover_is_tri_state_on_bad_data_and_strict_on_misuse():
    nan = float("nan")
    # NaN equity / missing notional / non-positive equity / negative magnitude
    for kwargs, expected in (
        (dict(traded_notional=[1.0, nan], equity=[10.0, 10.0]), "traded_notional_unavailable_at_period_1"),
        (dict(traded_notional=[1.0, 2.0], equity=[10.0, nan]), "equity_unavailable_at_period_1"),
        (dict(traded_notional=[1.0], equity=[0.0]), "equity_nonpositive_at_period_0"),
        (dict(traded_notional=[-1.0], equity=[10.0]), "negative_traded_notional_at_period_0"),
        (dict(traded_notional=[], equity=[]), "no_observations"),
    ):
        obs = annualized_turnover(**kwargs)
        assert obs.available is False
        assert obs.reason.startswith(expected), (obs.reason, expected)
        # unknown must never render as zero
        assert obs.annualized is None
        assert obs.one_way_annualized is None
        assert obs.traded_notional_total is None

    # Structural misuse (not data) raises, and says why.
    with pytest.raises(ValueError):
        annualized_turnover([1.0, 2.0], [10.0])
    with pytest.raises(TypeError):
        annualized_turnover([[1.0]], 10.0)


# ---------------------------------------------------------------------------
# 3. Both participation bases, hand-checkable
# ---------------------------------------------------------------------------
def test_volume_participation_reports_both_bases():
    # 50,000 shares against a 1,000,000-share session and a flat 400,000-share
    # 20-day baseline: 0.05 of today, 0.125 of the 20-day average.
    part = volume_participation(50_000.0, 1_000_000.0, volume_history=[400_000.0] * 20)
    assert part.available is True and part.reason is None
    assert part.of_daily_volume == pytest.approx(0.05, rel=1e-15)
    assert part.of_average_volume == pytest.approx(0.125, rel=1e-15)
    assert part.average_volume == pytest.approx(400_000.0, rel=1e-15)
    assert part.lookback == 20
    assert part.exceeds_daily_volume is False and part.exceeds_average_volume is False

    # Mixed history: mean of (10 x 1,000,000 + 10 x 500,000) = 750,000,
    # so 50,000 shares = 1/15 of the 20-day average.
    mixed = volume_participation(
        50_000.0, 1_000_000.0, volume_history=[1_000_000.0] * 10 + [500_000.0] * 10
    )
    assert mixed.average_volume == pytest.approx(750_000.0, rel=1e-15)
    assert mixed.of_average_volume == pytest.approx(50_000.0 / 750_000.0, rel=1e-12)

    # Only the LAST `lookback` sessions count: 10 huge sessions followed by 20
    # sessions at 200,000 give a 200,000 baseline, not the 30-day mean.
    windowed = volume_participation(
        50_000.0, 1_000_000.0,
        volume_history=[9_900_000.0] * 10 + [200_000.0] * 20,
    )
    assert windowed.average_volume == pytest.approx(200_000.0, rel=1e-15)
    assert windowed.of_average_volume == pytest.approx(0.25, rel=1e-15)

    # Both bases differ from each other (the point of reporting both).
    assert part.of_daily_volume != part.of_average_volume

    # Factual (not policy) over-size flags: 2x the whole session.
    oversized = volume_participation(2_000_000.0, 1_000_000.0, volume_history=[1_000_000.0] * 20)
    assert oversized.exceeds_daily_volume is True
    assert oversized.exceeds_average_volume is True

    # `lookback` is honoured: a 5-day basis over the same history.
    short = volume_participation(
        100_000.0, 400_000.0, volume_history=[100_000.0] * 5 + [900_000.0] * 15, lookback=5
    )
    assert short.average_volume == pytest.approx(900_000.0, rel=1e-15)
    assert short.lookback == 5


# ---------------------------------------------------------------------------
# 4. Missing volume -> tri-state, never 0 / mean fill
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs, reason_prefix",
    [
        (dict(traded_shares=100.0, daily_volume=None, volume_history=[1e6] * 20), "daily_volume_unavailable"),
        (dict(traded_shares=100.0, daily_volume=0.0, volume_history=[1e6] * 20), "daily_volume_zero_or_negative"),
        (dict(traded_shares=100.0, daily_volume=-5.0, volume_history=[1e6] * 20), "daily_volume_zero_or_negative"),
        (dict(traded_shares=100.0, daily_volume=float("nan"), volume_history=[1e6] * 20), "daily_volume_unavailable"),
        (dict(traded_shares=100.0, daily_volume=1e6, volume_history=None), "volume_history_unavailable"),
        (dict(traded_shares=100.0, daily_volume=1e6, volume_history=[]), "volume_history_unavailable"),
        (dict(traded_shares=100.0, daily_volume=1e6, volume_history=[1e6] * 19), "insufficient_volume_history"),
        (
            dict(traded_shares=100.0, daily_volume=1e6, volume_history=[1e6] * 19 + [0.0]),
            "volume_history_nonpositive",
        ),
        (
            dict(traded_shares=100.0, daily_volume=1e6, volume_history=[1e6] * 19 + [float("nan")]),
            "volume_history_unavailable_at_index",
        ),
        (dict(traded_shares=None, daily_volume=1e6, volume_history=[1e6] * 20), "traded_shares_unavailable"),
        (dict(traded_shares=float("nan"), daily_volume=1e6, volume_history=[1e6] * 20), "traded_shares_unavailable"),
        (dict(traded_shares=-1.0, daily_volume=1e6, volume_history=[1e6] * 20), "negative_traded_shares"),
    ],
)
def test_missing_volume_is_tri_state_and_never_zero_or_mean_filled(kwargs, reason_prefix):
    part = volume_participation(**kwargs)
    assert part.available is False
    assert part.reason.startswith(reason_prefix), (part.reason, reason_prefix)

    # THE core assertion: unobservable is `None`, NOT `0.0` and NOT a mean.
    for value in (part.of_daily_volume, part.of_average_volume, part.average_volume):
        assert value is None
    assert part.of_daily_volume != 0.0
    assert part.of_average_volume != 0.0
    assert part.exceeds_daily_volume is None
    assert part.exceeds_average_volume is None
    # the report agrees, and does not smuggle a number in either
    report = capacity_labels(participation=part)
    assert report["participation_label"] == "MISSING"
    assert report["participation_available"] is False
    assert report["participation_of_daily_volume"] is None
    assert report["participation_of_average_volume"] is None


def test_volume_participation_rejects_structural_misuse():
    with pytest.raises(ValueError):
        volume_participation(1.0, 100.0, volume_history=[1.0] * 20, lookback=0)
    with pytest.raises(TypeError):
        volume_participation(1.0, 100.0, volume_history=[[1.0] * 20])


# ---------------------------------------------------------------------------
# 5. Labels: advisory, strict boundaries, extreme values labelled not raised
# ---------------------------------------------------------------------------
def test_turnover_label_boundaries_are_strict_and_extremes_are_labelled():
    # L2.36 one-way lines: 8.0 合格 / 10.0 硬上限, strict `>` (恰 10.0 通过).
    assert turnover_label(0.0) == "OK"
    assert turnover_label(3.0) == "OK"
    assert turnover_label(8.0) == "OK"
    assert turnover_label(8.0 + 1e-9) == "WATCH"
    assert turnover_label(9.99) == "WATCH"
    assert turnover_label(10.0) == "WATCH"
    assert turnover_label(10.0 + 1e-9) == "BREACH"
    # Extreme but finite: labelled, never raised.
    assert turnover_label(1e9) == "BREACH"
    assert turnover_label(-1e9) == "OK"  # nonsensical, but negative is not a breach
    # Non-finite / missing is MISSING -- NaN must not fall through into OK.
    for bad in (None, float("nan"), float("inf"), float("-inf"), "n/a", object()):
        assert turnover_label(bad) == "MISSING", bad
    # A JSON-decoded numeric string is a value, not a gap.
    assert turnover_label("12.5") == "BREACH"


def test_capacity_labels_is_advisory_pure_and_json_safe():
    obs = annualized_turnover([5_000.0] * 4, 100_000.0)
    part = volume_participation(300_000.0, 1_000_000.0, volume_history=[500_000.0] * 20)

    report = capacity_labels(turnover=obs, participation=part)
    # machine-readable advisory contract
    assert report["advisory"] is True
    assert report["gates_orders"] is False
    assert "ADVISORY ONLY" in report["notes"][0]
    # both bases visible, and the basis of each is unambiguous
    assert report["turnover_convention"] == "two_way"
    assert report["turnover_two_way_annualized"] == pytest.approx(
        report["turnover_one_way_annualized"] * 2.0, rel=1e-12
    )
    assert report["turnover_label"] == "OK"
    assert report["participation_of_daily_volume"] == pytest.approx(0.3, rel=1e-15)
    assert report["participation_of_average_volume"] == pytest.approx(0.6, rel=1e-15)
    assert report["participation_label"] == "OBSERVED"
    assert report["turnover_thresholds"]["qualified_one_way_ann"] == 8.0
    assert report["turnover_thresholds"]["ceiling_one_way_ann"] == 10.0
    assert "L2.36" in report["turnover_thresholds"]["source"]

    # JSON-safe (this is what a live ticket / snapshot writer needs)
    assert json.loads(json.dumps(report))["turnover_label"] == "OK"

    # Pure: identical inputs -> identical report, and the frozen inputs are
    # untouched (no mutation, no accumulated state).
    again = capacity_labels(turnover=obs, participation=part)
    assert again == report
    assert obs == annualized_turnover([5_000.0] * 4, 100_000.0)
    assert part.of_daily_volume == pytest.approx(0.3, rel=1e-15)

    # No exceptions on absent / garbage inputs -- degradation, not a crash.
    empty = capacity_labels()
    assert empty["turnover_label"] == "MISSING"
    assert empty["participation_label"] == "MISSING"
    junk = capacity_labels(turnover=object(), participation=object())
    assert junk["turnover_label"] == "MISSING"
    assert junk["participation_label"] == "MISSING"
    # A duck-typed all-NaN observation is still MISSING, not OK.
    nan_turnover = lc.TurnoverObservation(
        available=True, convention="one_way", one_way_annualized=float("nan"), annualized=float("nan")
    )
    assert capacity_labels(turnover=nan_turnover)["turnover_label"] == "MISSING"
    # Extreme magnitudes produce BREACH + OBSERVED, never an exception.
    extreme_turnover = annualized_turnover([1e12], 1.0)
    extreme_part = volume_participation(1e12, 1.0, volume_history=[1.0] * 20)
    extreme = capacity_labels(turnover=extreme_turnover, participation=extreme_part)
    assert extreme["turnover_label"] == "BREACH"
    assert extreme["participation_label"] == "OBSERVED"
    assert extreme["participation_exceeds_daily_volume"] is True


def test_module_cannot_gate_orders_and_has_no_io():
    """Structural guard: no order-path import, no I/O, no threshold on output."""
    source = _module_source()
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"math", "dataclasses", "typing", "__future__"}, imported
    for forbidden in ("scripts", "web", "backtester", "allocation_backtester", "subprocess"):
        assert forbidden not in imported

    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & {"open", "print", "eval", "exec", "input", "compile"}), called

    # The report exposes no field that could be mistaken for an instruction.
    report_keys = set(capacity_labels().keys())
    assert not {k for k in report_keys if "order" in k and k != "gates_orders"}
    assert "gates_orders" in report_keys

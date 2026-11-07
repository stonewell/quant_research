"""Offline regression suite for `R6` -- the fixed-time overnight-exposure decision.

Unit under test: `common.overnight_exposure.decide_overnight_exposure` (read its module
docstring, not this file, if the two ever disagree -- the docstring is the frozen contract).

WHAT IS BEING PINNED (and what would turn these cases red)
----------------------------------------------------------
1. **The decision uses only bars that closed by 14:45**, so it cannot be a function of the
   price it acts on. The two price-taking moments in the engine are the 15:00 close
   (``execution_price="close"``) and the session TWAP whose last slice is at/after 14:45
   (``execution_price="intraday_twap"``). "``close`` and ``intraday_twap`` agree" is a
   **structural** claim, not an empirical one: ``decide_overnight_exposure`` takes no
   execution-price argument at all, so the two conventions feed it *identical* inputs and
   comparing the two "sequences" would be comparing the function to itself (a tautology, and
   not evidence). It is therefore asserted as structure
   (``test_decision_is_independent_of_execution_price_by_construction``), and the *testable*
   form of the look-ahead guard is the cutoff-ordering regression
   (``test_frozen_cutoff_precedes_both_price_taking_moments``): the frozen cutoff instant must
   precede both price-taking instants. Moving the default cutoff to 15:05 breaks this suite
   (mandatory reverse-breaking demonstration, `R6` item 4③).
2. **The boundary is inclusive** -- exactly ``-0.15`` triggers, ``-0.1499`` does not. This is
   why the module computes ``(close - running_high) / running_high`` and not
   ``close / running_high - 1.0``: only the former can represent the literal ``-0.15`` exactly
   for a float price pair, so the boundary case is a real number, not ``approx``.
3. **INSUFFICIENT_DATA is never 0.** An empty frame, a one-bar frame, a half-day session with
   no afternoon, and an all-NaN close column all yield ``scale is None`` / ``drawdown is None``.
   A ``0.0`` scale would liquidate the book and a ``0.0`` drawdown would read as "no drawdown";
   this suite asserts the literal ``0.0`` is absent from both fields.
4. **Determinism** -- the same inputs give an equal (frozen dataclass) result twice, and a feed
   stamped in another timezone with the same instants gives the same answer.

100% offline: synthetic 5m frames built here or by `common.intraday_synthetic` (`R3`, the
capability prerequisite). No network, no cache, no real market data, no `data/runs/**`, no
`backtests_research/`.

Acceptance mapping (`docs/development_plan.md` §2.20 `R6` item 4):
  ① trigger at the fixed time point -> `test_triggers_on_fixed_time_drawdown`
  ② not triggered is bit-identical  -> `test_not_triggered_is_bit_identical_scale`
  ③ close vs intraday_twap sequences agree **by construction** (no execution-price input; the
     testable guard is cutoff ordering -- cutoff moved after price-taking turns it red)
     -> `test_decision_is_independent_of_execution_price_by_construction`,
        `test_frozen_cutoff_precedes_both_price_taking_moments`,
        `test_decision_ignores_post_cutoff_bars`
  ④ timezone / half-day boundaries  -> `test_naive_index_rejected`,
     `test_insufficient_data_never_read_as_zero`, `test_utc_stamped_feed_yields_the_same_decision`
"""

from __future__ import annotations

import datetime as dt
import inspect
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.allocation_backtester import EXECUTION_PRICE_MODES
from common.intraday_synthetic import GRID_LABELS, build_synthetic_5m_panel
from common.overnight_exposure import (
    DECISIONS,
    DECISION_INSUFFICIENT_DATA,
    DECISION_NOT_TRIGGERED,
    DECISION_SPEC_VERSION,
    DECISION_TRIGGERED,
    DEFAULT_CUTOFF_TIME,
    DEFAULT_DRAWDOWN_THRESHOLD,
    DEFAULT_SCALE_ON_TRIGGER,
    INSUFFICIENT_REASON_CODES,
    SCALE_UNCHANGED,
    OvernightExposureDecision,
    decide_overnight_exposure,
)

TZ = "Asia/Shanghai"
DAY = dt.date(2024, 6, 3)
#: The A-share 5m session, borrowed from `R3` rather than re-declared: 48 END labels
#: (09:35 ... 11:30, 13:05 ... 15:00). A drift in the frozen grid must break this suite too.
LABELS = tuple(GRID_LABELS)
LAST_LABEL = LABELS[-1]                     # "15:00" -- the close convention's price moment
CUTOFF_LABEL = "14:45"                      # the END of the decision bar
assert LAST_LABEL == "15:00" and CUTOFF_LABEL in LABELS


# ---------------------------------------------------------------------------------------------
# helpers -- one market-level series, END-labelled, tz-aware
# ---------------------------------------------------------------------------------------------
def _index(day: dt.date, labels: tuple[str, ...] = LABELS) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([pd.Timestamp(f"{day.isoformat()} {label}", tz=TZ) for label in labels])


def _frame(day: dt.date, closes, highs=None, labels: tuple[str, ...] = LABELS) -> pd.DataFrame:
    """Build a single-session market series. ``highs`` defaults to ``closes`` (a zero range)."""
    closes = [float(c) for c in closes]
    assert len(closes) == len(labels), f"{len(closes)} closes vs {len(labels)} labels"
    highs = list(closes) if highs is None else [float(h) for h in highs]
    assert len(highs) == len(labels)
    return pd.DataFrame({"high": highs, "close": closes}, index=_index(day, labels))


def _set_at(frame: pd.DataFrame, label: str, value: float, column: str = "close") -> pd.DataFrame:
    """Return a copy with one bar's ``column`` replaced -- keeps the intent of each case readable."""
    ts = pd.Timestamp(f"{frame.index[0].date().isoformat()} {label}", tz=TZ)
    out = frame.copy()
    out.loc[ts, column] = float(value)
    return out


def _flat(day: dt.date, level: float = 100.0) -> pd.DataFrame:
    return _frame(day, [level] * len(LABELS))


# ---------------------------------------------------------------------------------------------
# ① trigger at the fixed time point
# ---------------------------------------------------------------------------------------------
def test_triggers_on_fixed_time_drawdown():
    """A -16% drawdown measured at the 14:45 close triggers a 0.5 market exposure coefficient."""
    frame = _set_at(_flat(DAY), CUTOFF_LABEL, 84.0)          # (84 - 100) / 100 = -0.16
    decision = decide_overnight_exposure(frame, DAY)

    assert decision.decision == DECISION_TRIGGERED
    assert decision.scale == 0.5 == DEFAULT_SCALE_ON_TRIGGER
    assert decision.drawdown == pytest.approx(-0.16)
    assert decision.running_high == pytest.approx(100.0)
    assert decision.cutoff_close == pytest.approx(84.0)
    assert decision.reason_code == "drawdown_at_or_below_threshold"
    # 09:35 .. 14:45 inclusive = 45 bars; the three post-cutoff bars are not evidence.
    assert decision.bars_used == 45
    # Same-session date and the resolved cutoff instant, in the session zone.
    assert decision.as_of_date == DAY
    assert decision.cutoff == pd.Timestamp(f"{DAY.isoformat()} {CUTOFF_LABEL}", tz=TZ)
    # The trigger is a market-level multiplier, never a per-symbol weight edit.
    assert decision.as_dict()["scale"] == 0.5


def test_trigger_ignores_the_size_of_the_later_collapse():
    """The decision is taken at 14:45: a -50% close at 15:00 must not deepen the reading."""
    frame = _set_at(_flat(DAY), CUTOFF_LABEL, 90.0)          # -10% at the decision point
    collapsed = _set_at(frame, LAST_LABEL, 50.0)             # -50% afterwards

    at_cutoff = decide_overnight_exposure(frame, DAY)
    after = decide_overnight_exposure(collapsed, DAY)

    assert at_cutoff.decision == DECISION_NOT_TRIGGERED      # -10% is inside -15%
    assert after == at_cutoff                                # the 15:00 print is not an input
    assert after.drawdown == pytest.approx(-0.10)


# ---------------------------------------------------------------------------------------------
# ② not triggered is bit-identical
# ---------------------------------------------------------------------------------------------
def test_not_triggered_is_bit_identical_scale():
    """Inside the threshold the module is a no-op: a literal 1.0, and a deterministic result."""
    frame = _set_at(_flat(DAY), CUTOFF_LABEL, 99.0)          # -1% at the decision point
    first = decide_overnight_exposure(frame, DAY)
    second = decide_overnight_exposure(frame, DAY)

    assert first.decision == DECISION_NOT_TRIGGERED
    assert first.scale == 1.0 == SCALE_UNCHANGED
    assert first.drawdown == pytest.approx(-0.01)
    assert first.reason_code == "drawdown_within_threshold"
    # "Not triggered changes nothing" has to be checkable, not prose: equality of the whole
    # frozen dataclass, plus a literal 1.0 multiplier (no rounding, no 0.9999).
    assert first == second
    assert first.as_dict()["scale"] == 1.0
    assert first.scale * 100.0 == 100.0


# ---------------------------------------------------------------------------------------------
# ③ INSUFFICIENT_DATA never reads as zero
# ---------------------------------------------------------------------------------------------
def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"high": pd.Series(dtype="float64"), "close": pd.Series(dtype="float64")},
        index=pd.DatetimeIndex([], tz=TZ),
    )


def _single_bar_frame() -> pd.DataFrame:
    return _frame(DAY, [100.0], labels=LABELS[:1])


def _half_day_frame() -> pd.DataFrame:
    # Morning only: 09:35 .. 11:30, no afternoon -> no bar ends at 14:45.
    morning = tuple(label for label in LABELS if label <= "11:30")
    assert len(morning) == 24 and CUTOFF_LABEL not in morning
    return _frame(DAY, [100.0] * len(morning), labels=morning)


def _all_nan_close_frame() -> pd.DataFrame:
    frame = _flat(DAY)
    frame["close"] = np.nan
    return frame


def _no_bars_for_date_frame() -> pd.DataFrame:
    return _frame(dt.date(2024, 6, 4), [100.0] * len(LABELS))


@pytest.mark.parametrize(
    ("builder", "expected_reason"),
    [
        (_empty_frame, "empty_frame"),
        (_single_bar_frame, "too_few_usable_bars"),
        (_half_day_frame, "no_bar_ending_at_cutoff"),
        (_all_nan_close_frame, "too_few_usable_bars"),
        (_no_bars_for_date_frame, "no_bars_for_date"),
    ],
    ids=["empty", "single_bar", "half_day", "all_nan_close", "no_bars_for_date"],
)
def test_insufficient_data_never_read_as_zero(builder, expected_reason):
    """Every "we cannot tell" path is explicit, and neither field is ever 0.0."""
    decision = decide_overnight_exposure(builder(), DAY)

    assert decision.decision == DECISION_INSUFFICIENT_DATA
    assert decision.scale is None
    assert decision.drawdown is None
    assert decision.reason_code == expected_reason
    assert decision.reason_code in INSUFFICIENT_REASON_CODES
    assert decision.reason                               # human text is present, not blank
    # The whole point: 0.0 would mean "liquidate" (scale) / "no drawdown" (drawdown).
    assert 0.0 not in {decision.scale, decision.drawdown}
    assert decision.as_dict()["scale"] is None
    assert decision.as_dict()["drawdown"] is None
    assert decision.cutoff == pd.Timestamp(f"{DAY.isoformat()} {CUTOFF_LABEL}", tz=TZ)


def test_insufficient_is_not_a_degenerate_not_triggered():
    """A half-day session and a shallow-drawdown session must not collapse to the same state."""
    shallow = decide_overnight_exposure(_set_at(_flat(DAY), CUTOFF_LABEL, 99.9), DAY)
    half_day = decide_overnight_exposure(_half_day_frame(), DAY)

    assert shallow.decision == DECISION_NOT_TRIGGERED
    assert half_day.decision == DECISION_INSUFFICIENT_DATA
    assert shallow != half_day
    assert shallow.scale == 1.0 and half_day.scale is None


# ---------------------------------------------------------------------------------------------
# ④ the boundary is inclusive
# ---------------------------------------------------------------------------------------------
def test_boundary_is_inclusive():
    """Exactly -0.15 triggers; -0.1499 does not. Pins ``<=``, not ``<``."""
    # (17 - 20) / 20 is exactly the float literal -0.15 -- verified below rather than assumed.
    exact = _frame(DAY, [20.0] * len(LABELS), highs=[20.0] * len(LABELS))
    exact = _set_at(exact, CUTOFF_LABEL, 17.0)
    inside = _set_at(_frame(DAY, [20.0] * len(LABELS), highs=[20.0] * len(LABELS)), CUTOFF_LABEL, 17.002)

    at_boundary = decide_overnight_exposure(exact, DAY)
    just_inside = decide_overnight_exposure(inside, DAY)

    assert at_boundary.drawdown == -0.15                     # exact equality, no approx
    assert at_boundary.decision == DECISION_TRIGGERED
    assert at_boundary.scale == 0.5

    assert just_inside.drawdown == pytest.approx(-0.1499, abs=1e-12)
    assert just_inside.drawdown > DEFAULT_DRAWDOWN_THRESHOLD
    assert just_inside.decision == DECISION_NOT_TRIGGERED
    assert just_inside.scale == 1.0

    # Anti-tautology: the two decisions really are separated by the comparison operator, so the
    # frozen threshold value itself must move them (inclusive on the trigger side).
    assert decide_overnight_exposure(exact, DAY, drawdown_threshold=-0.1499).decision == DECISION_TRIGGERED
    assert decide_overnight_exposure(exact, DAY, drawdown_threshold=-0.1501).decision == DECISION_NOT_TRIGGERED


def test_frozen_defaults_match_the_contract():
    """The owner-fixed convention is a literal here, so an accidental retune is visible."""
    assert DEFAULT_CUTOFF_TIME == dt.time(14, 45)
    assert DEFAULT_DRAWDOWN_THRESHOLD == -0.15
    assert DEFAULT_SCALE_ON_TRIGGER == 0.5
    assert DECISION_SPEC_VERSION == "r6.1"
    assert DECISIONS == ("TRIGGERED", "NOT_TRIGGERED", "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------------------------
# ③ dual-mode consistency -- structural, plus the cutoff-ordering regression that is testable
# ---------------------------------------------------------------------------------------------
_CRASH_SESSION = 2          # 0-based; the third of six sessions is repriced to a hard drawdown


def _multi_session_frame() -> tuple[pd.DataFrame, list[dt.date]]:
    """Six synthetic sessions (via the `R3` generator), one of them repriced to a -20% day.

    The generator gives a realistic bar shape; the crash is injected afterwards so that a
    triggered and a non-triggered session both appear in the panel -- a sequence that never
    triggers would make the cutoff-ordering regression a statement about an all-``INSUFFICIENT``
    (i.e. undecided) panel.
    """
    panel = build_synthetic_5m_panel(symbols=1, sessions=6, seed=20241106)
    frame = panel.bars[["ts", "high", "close"]].set_index("ts").sort_index()
    days = sorted({ts.date() for ts in frame.index})
    assert len(days) == 6

    crash_day = days[_CRASH_SESSION]
    afternoon = [ts.date() == crash_day and ts.strftime("%H:%M") >= "13:05" for ts in frame.index]
    frame.loc[afternoon, ["high", "close"]] = frame.loc[afternoon, ["high", "close"]] * 0.80
    return frame, days


def _price_taking_moment(day: dt.date, convention: str) -> pd.Timestamp:
    """When the engine takes the price for an order decided on ``day``.

    ``close``          -> the 15:00 close of the same session;
    ``intraday_twap``  -> the last session slice (the 15:00 bar), so the TWAP window's final
                          observation is at/after 14:45 -- the frozen justification for the
                          cutoff ordering.
    """
    assert convention in EXECUTION_PRICE_MODES, f"{convention!r} is not an engine mode"
    if convention == "close":
        return pd.Timestamp(f"{day.isoformat()} {LAST_LABEL}", tz=TZ)
    if convention == "intraday_twap":
        return pd.Timestamp(f"{day.isoformat()} {LABELS[-1]}", tz=TZ)
    raise AssertionError(f"{convention!r} has no price-taking moment in this suite")


def _decide_sequence(bars: pd.DataFrame, days: list[dt.date]):
    """The decision sequence over ``days``.

    Deliberately takes **no** price convention: ``decide_overnight_exposure`` has no execution-
    price input, so there is nothing a convention could change. An earlier version of this file
    accepted a ``convention`` argument and then ignored it for the decisions, which made its
    "both conventions agree" assertion a comparison of the function with itself.
    """
    return [decide_overnight_exposure(bars, day) for day in days]


def test_decision_is_independent_of_execution_price_by_construction():
    """The price mode cannot influence the decision -- because it is not a parameter at all.

    This is the *structural* reading of acceptance ③ (``close`` vs ``intraday_twap``). The two
    conventions differ only in which price the engine later books; the decision function never
    receives that price, so the equality of the two sequences is a fact about the signature,
    not a measurement. What is testable is asserted here: no price/mode/convention-shaped
    parameter exists, and passing one anyway is a ``TypeError`` rather than a silently ignored
    kwarg (a ``**kwargs`` signature would let a caller *believe* the mode was honoured).
    """
    parameters = inspect.signature(decide_overnight_exposure).parameters

    # A frozen, explicit signature -- not "no offending name found in whatever it happens to be".
    assert list(parameters) == [
        "bars",
        "as_of_date",
        "cutoff_time",
        "drawdown_threshold",
        "scale_on_trigger",
        "session_timezone",
    ]
    price_tokens = ("price", "mode", "convention", "execution")
    assert [name for name in parameters if any(t in name.lower() for t in price_tokens)] == []
    # No var-keyword escape hatch through which a mode could be smuggled in without a name.
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())

    frame = _set_at(_flat(DAY), CUTOFF_LABEL, 84.0)
    for mode in EXECUTION_PRICE_MODES:
        with pytest.raises(TypeError):
            decide_overnight_exposure(frame, DAY, execution_price=mode)
    with pytest.raises(TypeError):
        decide_overnight_exposure(frame, DAY, price_mode="intraday_twap")


def test_frozen_cutoff_precedes_both_price_taking_moments():
    """The frozen cutoff instant strictly precedes the 15:00 close and at/before the last TWAP slice.

    This is the *testable* form of the look-ahead guard: if the decision instant moved to (or
    past) a price-taking instant, the decision would ingest bars that only exist after the price
    it acts on was taken. A 15:05 (or 15:00) cutoff turns this red on the ordering assertion.

    The old dual-mode equality check is deliberately **not** repeated here: both "conventions"
    pass the same ``bars`` to the same signature, so ``close_decisions[i] == twap_decisions[i]``
    is a tautology and proves nothing. The structural claim lives in the test above.
    """
    bars, days = _multi_session_frame()
    decisions = _decide_sequence(bars, days)

    for index, day in enumerate(days):
        close_moment = _price_taking_moment(day, "close")
        twap_moment = _price_taking_moment(day, "intraday_twap")

        # The decision time point strictly precedes the 15:00 close ...
        assert decisions[index].cutoff < close_moment
        # ... and is at-or-before the last TWAP slice.
        assert decisions[index].cutoff <= twap_moment
        # The TWAP's final slice is itself at/after the frozen 14:45 cutoff (not earlier).
        assert twap_moment >= pd.Timestamp(f"{day.isoformat()} {CUTOFF_LABEL}", tz=TZ)

    # Panel sanity, so the sequence above is a real decided sequence and not six copies of one
    # INSUFFICIENT_DATA state (which would make the ordering claim vacuous, not wrong).
    states = [d.decision for d in decisions]
    assert DECISION_TRIGGERED in states
    assert DECISION_NOT_TRIGGERED in states
    assert len(decisions) == len(days) == 6


def test_decision_ignores_post_cutoff_bars():
    """Replacing every post-14:45 bar with an extreme value leaves the decision bit-identical."""
    base = _set_at(_flat(DAY), CUTOFF_LABEL, 84.0)           # -16% at the cutoff
    base_decision = decide_overnight_exposure(base, DAY)
    assert base_decision.decision == DECISION_TRIGGERED
    assert base_decision.drawdown == pytest.approx(-0.16)
    assert base_decision.bars_used == 45

    # After the cutoff: a running-high of 1e6 and a close of 1e-6. If any post-cutoff bar were
    # ingested, the drawdown would collapse towards -1.0.
    post = [ts > pd.Timestamp(f"{DAY.isoformat()} {CUTOFF_LABEL}", tz=TZ) for ts in base.index]
    mutated = base.copy()
    mutated.loc[post, "high"] = 1_000_000.0
    mutated.loc[post, "close"] = 1e-06

    mutated_decision = decide_overnight_exposure(mutated, DAY)

    assert mutated_decision == base_decision
    assert mutated_decision.drawdown == pytest.approx(-0.16)
    assert mutated_decision.running_high == pytest.approx(100.0)


# ---------------------------------------------------------------------------------------------
# ④ timezone and container boundaries
# ---------------------------------------------------------------------------------------------
def test_pre_session_bar_participates_in_the_running_high():
    """The frozen definition sets no 09:30 lower bound -- disclosed, so it is pinned here.

    A bar stamped before the open is a feed defect (`R3`'s ``pre_session`` class); the decision
    rule does not silently drop it, because a hidden filter inside the rule would be a second
    judgement that the contract never froze. Anyone who "helpfully" filters it turns this red.
    """
    frame = _flat(DAY)                                       # high == close == 100.0 all session
    assert decide_overnight_exposure(frame, DAY).running_high == pytest.approx(100.0)

    pre = pd.Timestamp(f"{DAY.isoformat()} 09:20", tz=TZ)
    extra = pd.DataFrame({"high": [110.0], "close": [110.0]}, index=pd.DatetimeIndex([pre]))
    with_pre = pd.concat([extra, frame]).sort_index()

    decision = decide_overnight_exposure(with_pre, DAY)
    assert decision.running_high == pytest.approx(110.0)
    assert decision.bars_used == 46                          # 45 session bars + the 09:20 bar
    assert decision.drawdown == pytest.approx((100.0 - 110.0) / 110.0)
    assert decision.decision == DECISION_NOT_TRIGGERED        # -9.09% is inside -15%


def test_naive_index_rejected():
    """A tz-naive index is refused: an unqualified 14:45 is an ambiguous instant."""
    naive = _flat(DAY)
    naive.index = naive.index.tz_localize(None)
    assert naive.index.tz is None

    with pytest.raises(ValueError) as excinfo:
        decide_overnight_exposure(naive, DAY)

    message = str(excinfo.value)
    assert "tz-naive" in message
    assert "tz_localize" in message                      # the message says what to do about it
    assert "Asia/Shanghai" in message                    # ... and names the frozen session zone


def test_utc_stamped_feed_yields_the_same_decision():
    """The same instants labelled in UTC must decide identically (instant, not wall clock)."""
    local = _set_at(_flat(DAY), CUTOFF_LABEL, 84.0)
    utc = local.copy()
    utc.index = utc.index.tz_convert("UTC")

    assert decide_overnight_exposure(utc, DAY) == decide_overnight_exposure(local, DAY)


def test_duplicate_timestamps_rejected():
    """Two closes for one bar have no defensible 'the' value."""
    frame = _flat(DAY)
    duplicated = pd.concat([frame, frame.iloc[[-1]]])
    assert duplicated.index.has_duplicates

    with pytest.raises(ValueError, match="duplicate"):
        decide_overnight_exposure(duplicated, DAY)


def test_inconsistent_decisions_cannot_be_constructed():
    """The frozen dataclass refuses a state the rule can never produce (INSUFFICIENT with 0.0)."""
    with pytest.raises(ValueError, match="INSUFFICIENT_DATA"):
        OvernightExposureDecision(
            as_of_date=DAY,
            decision=DECISION_INSUFFICIENT_DATA,
            scale=0.0,
            drawdown=None,
            cutoff=pd.Timestamp(f"{DAY.isoformat()} {CUTOFF_LABEL}", tz=TZ),
            bars_used=0,
            reason_code="too_few_usable_bars",
            reason="hand-built, must be refused",
        )
    with pytest.raises(ValueError, match="closed vocabulary"):
        OvernightExposureDecision(
            as_of_date=DAY,
            decision="MAYBE",
            scale=1.0,
            drawdown=-0.01,
            cutoff=pd.Timestamp(f"{DAY.isoformat()} {CUTOFF_LABEL}", tz=TZ),
            bars_used=45,
            reason_code="drawdown_within_threshold",
            reason="hand-built, must be refused",
        )

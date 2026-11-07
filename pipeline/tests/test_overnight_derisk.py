"""Offline regression suite for `R6` (`r6.3`) -- the overnight **de-risking** judgement.

Unit under test: `common.overnight_derisk` (read its module docstring and the frozen judgement
`docs/overnight_derisk_judgement_freeze.md`, not this file, if the two ever disagree).

WHAT IS PINNED (and what must turn red)
---------------------------------------
1. `TRIGGERED` -> non-cash weights exactly `x 0.5`, cash proxy untouched, an explicit `0.0` stays
   `0.0`, and `sum` drops by exactly the de-risked amount (**no renormalisation**).
2. `NOT_TRIGGERED` -> the returned mapping equals the input float-for-float (bit-identical), i.e.
   the Stage-2 path is unchanged.
3. `INSUFFICIENT_DATA` -> no downscale, explicit fields, **exactly one** `RuntimeWarning`, and
   **not equal** to `NOT_TRIGGERED` (the whole point of the third state).
4. The decision instant is a *derived* instant: `D >= close` (mutation: `minutes_before_close=-5`)
   raises instead of silently deciding after the price was taken.
5. Half-day + timezone: US half-day 13:00 -> 12:15, HK half-day 12:00 -> 11:15, A-share full day
   15:00 -> 14:15, all tz-aware market-local.
6. No look-ahead in the *inputs*: `gap_t` consumes only `Open_t` and `Close_{t-1}`, so perturbing
   `Close_t` must not change `gap_t` (it may change `gap_{t+1}`).
7. (`r6.3`, freeze doc `## Revision 2026-11-07b`) The sigma cross-section is the **loaded universe
   for the market minus the cash proxies**, NOT the target book: the symbol set passed to
   `compute_gap_series` is identical when only the target book changes (rotation invariance), and
   cash proxies present in the universe never enter `N_t`.

100% offline: synthetic frames only. No network, no cache, no real market data, no
`data/runs/**`, no `backtests_research/`.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import warnings
from zoneinfo import ZoneInfo

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.overnight_derisk import (
    DECISION_SPEC_VERSION,
    DEFAULT_BASELINE_WINDOW,
    DEFAULT_DOWNSCALE_RATIO,
    DEFAULT_GAP_WINDOW,
    DEFAULT_MINUTES_BEFORE_CLOSE,
    DEFAULT_SIGMA_MULTIPLIER,
    RATIO_UNCHANGED,
    STATUSES,
    OvernightDeriskConfig,
    OvernightDeriskDecision,
    OvernightDeriskStatus,
    apply_overnight_downscale,
    assert_decision_precedes_pricing,
    compute_gap_series,
    evaluate_overnight_derisk,
    resolve_decision_instant,
    session_pricing_point,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gap_series(
    n: int = 320, base: float = 0.004, spike: float | None = 0.06, end: str | None = None
) -> pd.Series:
    """Alternating small gaps (stable sigma) with an optional 20-day spike at the tail.

    `end` anchors the series so its **last** observation is the session being judged -- the
    Stage-2 wiring test needs that, because `evaluate_overnight_derisk` truncates the gap series
    at `session_date` (a spike placed after the as-of date would correctly be ignored).
    """
    idx = pd.bdate_range(end=end, periods=n) if end else pd.bdate_range("2024-01-01", periods=n)
    values = np.empty(n, dtype="float64")
    values[0::2] = base
    values[1::2] = -base
    if spike is not None and n >= DEFAULT_GAP_WINDOW:
        tail = np.empty(DEFAULT_GAP_WINDOW, dtype="float64")
        tail[0::2] = spike
        tail[1::2] = -spike
        values[-DEFAULT_GAP_WINDOW:] = tail
    return pd.Series(values, index=idx, name="gap")


def _symbols_used(gap: pd.Series, n: int = 4) -> pd.Series:
    return pd.Series(n, index=gap.index, name="symbols_used", dtype="int64")


def _targets() -> dict[str, float]:
    # 0.6 risky + 0.4 cash proxy; the A-share cash proxy default is 511880, the US one BIL.
    return {"600000.SH": 0.30, "601398.SH": 0.20, "000001.SZ": 0.10, "511880.SH": 0.0, "BIL": 0.40}


# --------------------------------------------------------------------------- #
# 1. TRIGGERED
# --------------------------------------------------------------------------- #
def test_triggered_scales_only_non_cash_and_never_renormalises():
    gap = _gap_series(spike=0.06)
    decision = evaluate_overnight_derisk(
        gap, _symbols_used(gap), "china", gap.index[-1].date()
    )
    assert decision.status is OvernightDeriskStatus.TRIGGERED
    assert decision.ratio == DEFAULT_DOWNSCALE_RATIO == 0.5
    assert decision.sigma_gap is not None and decision.baseline_median is not None
    assert decision.sigma_gap > DEFAULT_SIGMA_MULTIPLIER * decision.baseline_median

    targets = _targets()
    scaled = apply_overnight_downscale(targets, decision, {"BIL"})

    # Risky weights exactly halved (float-exact for these dyadic values).
    assert scaled["600000.SH"] == 0.15
    assert scaled["601398.SH"] == 0.10
    assert scaled["000001.SZ"] == 0.05
    # Cash proxy untouched, and an explicit 0.0 on a non-cash symbol stays a literal 0.0.
    assert scaled["BIL"] == 0.40
    assert scaled["511880.SH"] == 0.0
    assert is_not_negative_zero(scaled["511880.SH"])

    # No renormalisation: the total drops by exactly half the risky weight (0.6 -> 0.3).
    assert sum(scaled.values()) == pytest.approx(0.70)
    assert sum(targets.values()) - sum(scaled.values()) == pytest.approx(0.30)
    # ... and the residual is NOT pushed back into the cash proxy.
    assert scaled["BIL"] == targets["BIL"]

    # Deterministic / pure.
    again = evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())
    assert again == decision


def is_not_negative_zero(value: float) -> bool:
    """`0.0 == -0.0` in Python, so pin the sign bit explicitly."""
    return not (value == 0.0 and str(value).startswith("-"))


def test_cash_proxy_respects_every_supplied_symbol():
    gap = _gap_series(spike=0.06)
    decision = evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())
    scaled = apply_overnight_downscale(
        {"600000.SH": 0.5, "511880": 0.5}, decision, {"511880", "BIL", "GC001"}
    )
    assert scaled == {"600000.SH": 0.25, "511880": 0.5}


def test_nan_weight_is_refused_not_propagated():
    gap = _gap_series(spike=0.06)
    decision = evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())
    with pytest.raises(ValueError, match="NaN"):
        apply_overnight_downscale({"600000.SH": float("nan")}, decision, {"BIL"})


# --------------------------------------------------------------------------- #
# 2. NOT_TRIGGERED -> bit-identical
# --------------------------------------------------------------------------- #
def test_not_triggered_is_bit_identical_to_the_input():
    gap = _gap_series(spike=None)
    decision = evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())
    assert decision.status is OvernightDeriskStatus.NOT_TRIGGERED
    assert decision.ratio == RATIO_UNCHANGED == 1.0
    assert decision.sigma_gap is not None and decision.baseline_median is not None
    assert decision.sigma_gap < DEFAULT_SIGMA_MULTIPLIER * decision.baseline_median

    targets = _targets()
    out = apply_overnight_downscale(targets, decision, {"BIL"})
    assert out == targets
    for key, value in targets.items():
        assert out[key] == value  # float-for-float
    assert list(out) == list(targets)  # no key reordering / dropping
    assert sum(out.values()) == sum(targets.values())


# --------------------------------------------------------------------------- #
# 3. INSUFFICIENT_DATA (never 0, never NOT_TRIGGERED)
# --------------------------------------------------------------------------- #
def test_insufficient_gap_window_is_its_own_state():
    gap = _gap_series(n=DEFAULT_GAP_WINDOW - 5, spike=None)
    with pytest.warns(RuntimeWarning, match="INSUFFICIENT_DATA"):
        decision = evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())

    assert decision.status is OvernightDeriskStatus.INSUFFICIENT_DATA
    assert decision.status is not OvernightDeriskStatus.NOT_TRIGGERED
    assert decision.status.value != OvernightDeriskStatus.NOT_TRIGGERED.value
    assert decision.ratio == RATIO_UNCHANGED
    assert decision.sigma_gap is None and decision.baseline_median is None
    assert decision.sessions_used == DEFAULT_GAP_WINDOW - 5
    assert "INSUFFICIENT_DATA" in decision.reason

    targets = _targets()
    assert apply_overnight_downscale(targets, decision, {"BIL"}) == targets

    block = decision.ticket_block()
    assert block["status"] == "INSUFFICIENT_DATA"
    assert block["ratio_applied"] == 1.0
    assert block["sigma_gap"] is None and block["baseline_median"] is None
    assert block["reason"]
    # The tri-state is closed: no other spelling may ever appear.
    assert block["status"] in STATUSES


def test_insufficient_baseline_window_is_its_own_state():
    # 200 usable gaps >= 20 (so sigma is computable) but the sigma history is 181 < 252.
    gap = _gap_series(n=200, spike=None)
    with pytest.warns(RuntimeWarning, match="INSUFFICIENT_DATA"):
        decision = evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())

    assert decision.status is OvernightDeriskStatus.INSUFFICIENT_DATA
    assert decision.sigma_gap is not None  # sigma exists; the *baseline* does not
    assert decision.baseline_median is None
    assert decision.ratio == RATIO_UNCHANGED
    assert decision.sessions_used == 200
    # Distinguishable from "not triggered" on every field a consumer might read.
    assert (decision.status, decision.baseline_median) != (
        OvernightDeriskStatus.NOT_TRIGGERED,
        decision.sigma_gap,
    )
    assert "基线" in decision.reason


def test_empty_gap_series_is_insufficient_data_not_zero():
    empty = pd.Series(dtype="float64", index=pd.DatetimeIndex([]))
    with pytest.warns(RuntimeWarning):
        decision = evaluate_overnight_derisk(
            empty, pd.Series(dtype="int64", index=pd.DatetimeIndex([])), "china", dt.date(2025, 12, 1)
        )
    assert decision.status is OvernightDeriskStatus.INSUFFICIENT_DATA
    assert decision.ratio == 1.0
    assert decision.sigma_gap is None and decision.baseline_median is None
    assert decision.sessions_used == 0


# --------------------------------------------------------------------------- #
# 4. Decision-instant geometry guard
# --------------------------------------------------------------------------- #
def test_decision_instant_must_precede_the_close():
    day = dt.date(2025, 12, 1)  # normal US session, 16:00 close
    # The frozen derivation is legal ...
    assert resolve_decision_instant("us", day) < session_pricing_point("us", day, "close")
    # ... and an instant pushed past the close is refused (never clamped, never silent).
    with pytest.raises(ValueError, match="at/after the .* close"):
        resolve_decision_instant("us", day, -5)
    # Pushed before the open, also refused.
    with pytest.raises(ValueError, match="at/before the .* open"):
        resolve_decision_instant("us", day, 10_000)


def test_evaluate_refuses_a_decision_instant_after_the_close():
    gap = _gap_series(spike=None)
    bad = OvernightDeriskConfig(minutes_before_close=-5)
    with pytest.raises(ValueError):
        evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date(), bad)


def test_ordering_guard_rejects_a_pricing_point_at_or_before_the_decision():
    tz = ZoneInfo("Asia/Shanghai")
    t = dt.datetime(2025, 12, 1, 14, 15, tzinfo=tz)
    assert_decision_precedes_pricing(t, dt.datetime(2025, 12, 1, 15, 0, tzinfo=tz))
    with pytest.raises(ValueError, match="not strictly"):
        assert_decision_precedes_pricing(t, t)
    with pytest.raises(ValueError):
        assert_decision_precedes_pricing(t, dt.datetime(2025, 12, 1, 14, 0, tzinfo=tz))
    with pytest.raises(ValueError):
        assert_decision_precedes_pricing(dt.datetime(2025, 12, 1, 14, 15), t)  # naive


def test_session_date_must_be_a_plain_date():
    with pytest.raises(TypeError):
        resolve_decision_instant("china", dt.datetime(2025, 12, 1, 15, 0))


def test_decision_instant_refuses_non_integral_minutes():
    """No silent truncation: `45.5` must not decide at 14:14:30 while claiming 45 minutes.

    `OvernightDeriskConfig` already rejected a non-`int`; the free function used to
    `int(minutes_before_close)` the float, so the one entry point that is callable directly could
    drift by up to 59 seconds without telling anyone. An integral float (`45.0`) is still the value
    it unambiguously denotes.
    """
    day = dt.date(2025, 12, 1)
    assert resolve_decision_instant("us", day, 45.0) == resolve_decision_instant("us", day, 45)
    for bad in (45.5, 0.5, float("nan"), float("inf"), True, "45", None):
        with pytest.raises(ValueError):
            resolve_decision_instant("us", day, bad)
    # The geometry guard still owns negatives (that is the ordering mutation, not a type error).
    with pytest.raises(ValueError, match="at/after the .* close"):
        resolve_decision_instant("us", day, -5.0)


# --------------------------------------------------------------------------- #
# 5. Half-day + timezone
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "market,day,expected_local,zone",
    [
        # CHINA_EARLY_CLOSES is empty for every covered year: a full 15:00 day -> 14:15.
        ("china", dt.date(2025, 12, 24), dt.time(14, 15), "Asia/Shanghai"),
        # US half-day (day after Thanksgiving 2025-11-28, 13:00 close) -> 12:15.
        ("us", dt.date(2025, 11, 28), dt.time(12, 15), "America/New_York"),
        # US full session -> 15:15.
        ("us", dt.date(2025, 12, 1), dt.time(15, 15), "America/New_York"),
        # HK half-day (Christmas Eve 2025, 12:00 close) -> 11:15.
        ("hk", dt.date(2025, 12, 24), dt.time(11, 15), "Asia/Hong_Kong"),
        # HK full session -> 15:15.
        ("hk", dt.date(2025, 12, 1), dt.time(15, 15), "Asia/Hong_Kong"),
    ],
)
def test_decision_instant_is_derived_from_the_session_close(market, day, expected_local, zone):
    instant = resolve_decision_instant(market, day)
    assert instant.tzinfo is not None, "must be tz-aware market-local, never naive"
    assert instant.utcoffset() is not None
    assert instant.astimezone(ZoneInfo(zone)) == instant  # already in the market's own zone
    assert (instant.date(), instant.time()) == (day, expected_local)
    # It is derived, not hard-coded: the offset from the close is exactly the frozen 45 minutes.
    close = session_pricing_point(market, day, "close")
    assert (close - instant) == dt.timedelta(minutes=DEFAULT_MINUTES_BEFORE_CLOSE)


def test_half_day_moves_the_instant_earlier_not_later():
    full = resolve_decision_instant("us", dt.date(2025, 12, 1))
    half = resolve_decision_instant("us", dt.date(2025, 11, 28))
    # A 13:00 close must move the decision to 12:15, never leave it at the 15:15 full-day answer
    # (which would be 2h15m AFTER that day's close -> a price-taking look-ahead).
    assert half.time() == dt.time(12, 15)
    assert half < session_pricing_point("us", dt.date(2025, 11, 28), "close")
    assert half.time() != full.time()


def test_intraday_twap_pricing_point_is_not_before_the_decision():
    for market, day in (("china", dt.date(2025, 12, 24)), ("hk", dt.date(2025, 12, 24))):
        decision = resolve_decision_instant(market, day)
        twap_point = session_pricing_point(market, day, "intraday_twap")
        assert_decision_precedes_pricing(decision, twap_point)
        assert decision < twap_point
    with pytest.raises(ValueError):
        session_pricing_point("china", dt.date(2025, 12, 24), "next_open")


# --------------------------------------------------------------------------- #
# 6. No look-ahead in the inputs
# --------------------------------------------------------------------------- #
def _three_day_universe(close_t: float = 100.0) -> dict[str, pd.DataFrame]:
    dates = pd.DatetimeIndex(["2025-12-01", "2025-12-02", "2025-12-03"])
    return {
        "600000.SH": pd.DataFrame(
            {"Open": [100.0, 101.0, 102.0], "Close": [100.5, 101.5, close_t]}, index=dates
        ),
        "601398.SH": pd.DataFrame(
            {"Open": [50.0, 50.5, 51.0], "Close": [50.2, 50.8, 51.4]}, index=dates
        ),
    }


def test_gap_uses_only_the_open_of_t_and_the_close_of_t_minus_one():
    base, used = compute_gap_series(_three_day_universe(), "2025-12-03")
    t = pd.Timestamp("2025-12-03")
    expected = (
        (102.0 / 101.5 - 1.0) + (51.0 / 50.8 - 1.0)
    ) / 2
    assert base.loc[t] == pytest.approx(expected)
    assert int(used.loc[t]) == 2

    # Perturbing Close_t (the as-of bar's own close) must NOT move gap_t ...
    perturbed, perturbed_used = compute_gap_series(_three_day_universe(close_t=1.0), "2025-12-03")
    assert perturbed.loc[t] == base.loc[t]
    assert int(perturbed_used.loc[t]) == int(used.loc[t])

    # ... but Close_t IS the prior close for t+1, so gap_{t+1} does read it (this half of the
    # assertion is what proves the test is not merely asserting "a constant").
    dates = pd.DatetimeIndex(["2025-12-01", "2025-12-02", "2025-12-03", "2025-12-04"])
    normal = {
        "600000.SH": pd.DataFrame(
            {"Open": [100.0, 101.0, 102.0, 103.0], "Close": [100.5, 101.5, 102.5, 103.5]},
            index=dates,
        )
    }
    crashed = {
        "600000.SH": pd.DataFrame(
            {"Open": [100.0, 101.0, 102.0, 103.0], "Close": [100.5, 101.5, 50.0, 103.5]},
            index=dates,
        )
    }
    gap_normal, _ = compute_gap_series(normal, "2025-12-04")
    gap_crashed, _ = compute_gap_series(crashed, "2025-12-04")
    assert gap_crashed.loc[pd.Timestamp("2025-12-03")] == gap_normal.loc[pd.Timestamp("2025-12-03")]
    assert gap_crashed.loc[pd.Timestamp("2025-12-04")] != gap_normal.loc[pd.Timestamp("2025-12-04")]


def test_gap_excludes_symbols_without_two_adjacent_bars_and_never_fills_zero():
    dates = pd.DatetimeIndex(["2025-12-01", "2025-12-02", "2025-12-03"])
    universe = {
        # Complete: contributes on 12-02 and 12-03.
        "600000.SH": pd.DataFrame(
            {"Open": [100.0, 101.0, 102.0], "Close": [100.5, 101.5, 102.5]}, index=dates
        ),
        # Missing the 12-02 bar entirely: cannot contribute on 12-02 nor on 12-03 (no prior close).
        "601398.SH": pd.DataFrame({"Open": [50.0, 51.0], "Close": [50.2, 51.2]}, index=[dates[0], dates[2]]),
        # No Close column at all -> dropped up front, counted nowhere.
        "000001.SZ": pd.DataFrame({"Open": [10.0, 10.1, 10.2]}, index=dates),
        # Missing Open on the last day -> excluded that day only.
        "600519.SH": pd.DataFrame(
            {"Open": [1.0, 2.0, np.nan], "Close": [1.5, 2.5, 3.5]}, index=dates
        ),
    }
    gap, used = compute_gap_series(universe, "2025-12-03")
    # 12-02: 600000.SH + 600519.SH (both have Open_t and Close_{t-1}); 601398.SH has no bar
    # that day and 000001.SZ has no Close column at all.
    assert int(used.loc[pd.Timestamp("2025-12-02")]) == 2
    # 12-03: only 600000.SH -- 601398.SH has no *12-02* bar (so no adjacent prior close) and
    # 600519.SH's Open is missing that day. Both are excluded, never imputed.
    assert int(used.loc[pd.Timestamp("2025-12-03")]) == 1
    # Exclusion is not imputation: the mean is over the contributing symbols only (here: one).
    assert gap.loc[pd.Timestamp("2025-12-03")] == pytest.approx(102.0 / 101.5 - 1.0)

    # A day where every symbol lacks a usable prior close is NaN, NOT 0.0.
    empty_day = pd.DataFrame(
        {"Open": [100.0, 101.0], "Close": [100.5, 101.5]}, index=pd.DatetimeIndex(["2025-12-04", "2025-12-05"])
    )
    gap2, used2 = compute_gap_series({"600000.SH": empty_day}, "2025-12-04")
    assert int(used2.loc[pd.Timestamp("2025-12-04")]) == 0
    assert pd.isna(gap2.loc[pd.Timestamp("2025-12-04")])
    assert gap2.loc[pd.Timestamp("2025-12-04")] != 0.0


def test_gap_series_truncates_at_as_of_and_is_never_negative_zero():
    gap, used = compute_gap_series(_three_day_universe(), "2025-12-02")
    assert gap.index.max() == pd.Timestamp("2025-12-02")
    assert used.index.max() == pd.Timestamp("2025-12-02")
    # A perfectly flat open is a real 0.0 gap and must survive as 0.0 (not NaN, not -0.0).
    dates = pd.DatetimeIndex(["2025-12-01", "2025-12-02"])
    flat = {"600000.SH": pd.DataFrame({"Open": [10.0, 10.0], "Close": [10.0, 10.0]}, index=dates)}
    gap3, used3 = compute_gap_series(flat, "2025-12-02")
    assert gap3.loc[pd.Timestamp("2025-12-02")] == 0.0
    assert int(used3.loc[pd.Timestamp("2025-12-02")]) == 1


# --------------------------------------------------------------------------- #
# Config validation / frozen constants
# --------------------------------------------------------------------------- #
def test_frozen_defaults_and_spec_version():
    cfg = OvernightDeriskConfig()
    # `r6.3` (freeze doc `## Revision 2026-11-07b`): only the sigma cross-section source moved
    # (loaded universe minus cash proxies, instead of the target book). Every frozen number is
    # unchanged, and this assertion pins the tag bump so a silent revert of the source is visible.
    assert DECISION_SPEC_VERSION == "r6.3"
    assert cfg.minutes_before_close == 45
    assert cfg.gap_window == 20
    assert cfg.baseline_window == 252
    assert cfg.sigma_multiplier == 2.0
    assert cfg.downscale_ratio == 0.5
    assert cfg.enabled is True
    assert STATUSES == ("TRIGGERED", "NOT_TRIGGERED", "INSUFFICIENT_DATA")


@pytest.mark.parametrize("ratio", [0.0, 1.0, -0.5, 1.5, float("nan")])
def test_downscale_ratio_must_be_strictly_inside_the_unit_interval(ratio):
    with pytest.raises(ValueError, match="0 < ratio < 1"):
        OvernightDeriskConfig(downscale_ratio=ratio)


@pytest.mark.parametrize("kwargs", [{"gap_window": 1}, {"baseline_window": 0}, {"gap_window": 0}])
def test_windows_must_exceed_one(kwargs):
    with pytest.raises(ValueError):
        OvernightDeriskConfig(**kwargs)


def test_ticket_block_carries_every_frozen_field():
    gap = _gap_series(spike=0.06)
    decision = evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())
    block = decision.ticket_block()
    for key in (
        "status",
        "decision_instant",
        "sigma_gap",
        "baseline_median",
        "sigma_multiplier",
        "ratio_applied",
        "sessions_used",
        "symbols_used",
        "reason",
    ):
        assert key in block, key
    assert dt.datetime.fromisoformat(block["decision_instant"]).tzinfo is not None
    assert block["ratio_applied"] == DEFAULT_DOWNSCALE_RATIO * 1.0  # 0.5
    assert block["sigma_multiplier"] == DEFAULT_SIGMA_MULTIPLIER
    assert block["symbols_used"] == 4


def test_decision_object_refuses_to_lie_about_its_state():
    instant = resolve_decision_instant("china", dt.date(2025, 12, 1))
    base = dict(
        market="china",
        session_date=dt.date(2025, 12, 1),
        decision_instant=instant,
        sessions_used=300,
        symbols_used=4,
        reason="constructed",
    )
    # INSUFFICIENT_DATA may never carry a de-risking ratio.
    with pytest.raises(ValueError):
        OvernightDeriskDecision(
            status=OvernightDeriskStatus.INSUFFICIENT_DATA,
            ratio=0.5,
            sigma_gap=None,
            baseline_median=None,
            **base,
        )
    # TRIGGERED must carry a real reading (no "triggered without evidence").
    with pytest.raises(ValueError):
        OvernightDeriskDecision(
            status=OvernightDeriskStatus.TRIGGERED,
            ratio=0.5,
            sigma_gap=None,
            baseline_median=None,
            **base,
        )
    # ... and a non-triggered state may not smuggle in a multiplier.
    with pytest.raises(ValueError):
        OvernightDeriskDecision(
            status=OvernightDeriskStatus.NOT_TRIGGERED,
            ratio=0.9,
            sigma_gap=0.01,
            baseline_median=0.01,
            **base,
        )


def test_insufficient_data_warns_once_per_evaluation():
    gap = _gap_series(n=10, spike=None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())
    runtime = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    # EXACTLY one: a per-symbol / per-day flood would still "contain a warning" and pass the old
    # `any(...)` assertion, while drowning the operator in noise on every insufficient session.
    assert len(runtime) == 1, f"expected exactly one RuntimeWarning, got {len(runtime)}"
    assert "INSUFFICIENT_DATA" in str(runtime[0].message)


def test_triggered_does_not_warn():
    gap = _gap_series(spike=0.06)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        evaluate_overnight_derisk(gap, _symbols_used(gap), "china", gap.index[-1].date())
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]


# --------------------------------------------------------------------------- #
# Stage-2 wiring (script-level, still 100% offline)
# --------------------------------------------------------------------------- #
def test_stage2_ticket_applies_the_triggered_downscale(tmp_path, monkeypatch):
    """The runner multiplies the real target book and stamps every affected row.

    `compute_gap_series` is replaced at the *script's* import site with a spiking series, so the
    verdict is TRIGGERED without needing a ~300-session synthetic panel that happens to gap; the
    rest of the Stage-2 path (weights -> gates -> share math -> ticket) runs for real against the
    offline synthetic provider.
    """
    import json

    import scripts.run_live_strategy as runner

    universe_file = tmp_path / "univ.txt"
    universe_file.write_text("600000.SH\n601398.SH\n", encoding="utf-8")

    gap = _gap_series(spike=0.06, end="2024-05-10")
    monkeypatch.setattr(
        runner,
        "compute_gap_series",
        lambda universe, as_of_date: (gap, _symbols_used(gap)),
    )

    runner.main(
        [
            "--strategy",
            "equal_weight",
            "--universe-file",
            str(universe_file),
            "--output-dir",
            str(tmp_path),
            "--as-of-date",
            "2024-05-10",
            "--start-date",
            "2024-01-01",
            "--data-provider",
            "synthetic",
            "--portfolio-value",
            "100000",
        ]
    )

    payload = json.loads((tmp_path / "live_trading_ticket.json").read_text(encoding="utf-8"))
    rows = [r for r in payload["orders"] if r["symbol"] in {"600000.SH", "601398.SH"}]
    assert {r["symbol"] for r in rows} == {"600000.SH", "601398.SH"}
    for row in rows:
        # equal_weight => 0.5 each before de-risking; TRIGGERED halves both.
        assert row["target_weight"] == pytest.approx(0.25)
        block = row["detail_reason"]["overnight_derisk"]
        assert block["status"] == "TRIGGERED"
        assert block["ratio_applied"] == 0.5
        assert block["sigma_multiplier"] == 2.0
        assert block["decision_instant"].endswith("+08:00")

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["overnight_derisk"]["status"] == "TRIGGERED"
    assert summary["overnight_derisk"]["cash_proxy_symbols"] == []
    # The early clock/freshness block must survive the second (merge) write.
    assert summary["clock"]["probe_attempts"] >= 1


def test_stage2_flag_off_leaves_the_book_untouched(tmp_path, monkeypatch):
    import json

    import scripts.run_live_strategy as runner

    universe_file = tmp_path / "univ.txt"
    universe_file.write_text("600000.SH\n601398.SH\n", encoding="utf-8")

    def _boom(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("--no-enable-overnight-derisk must not evaluate the judgement")

    monkeypatch.setattr(runner, "compute_gap_series", _boom)

    runner.main(
        [
            "--strategy",
            "equal_weight",
            "--universe-file",
            str(universe_file),
            "--output-dir",
            str(tmp_path),
            "--as-of-date",
            "2024-05-10",
            "--start-date",
            "2024-01-01",
            "--data-provider",
            "synthetic",
            "--portfolio-value",
            "100000",
            "--no-enable-overnight-derisk",
        ]
    )

    payload = json.loads((tmp_path / "live_trading_ticket.json").read_text(encoding="utf-8"))
    rows = [r for r in payload["orders"] if r["symbol"] in {"600000.SH", "601398.SH"}]
    assert len(rows) == 2
    for row in rows:
        assert row["target_weight"] == pytest.approx(0.5)
        assert "overnight_derisk" not in (row.get("detail_reason") or {})
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert "overnight_derisk" not in summary


# --------------------------------------------------------------------------- #
# r6.3 -- the sigma cross-section is the loaded universe, not the target book
# (freeze doc `## Revision 2026-11-07b`)
# --------------------------------------------------------------------------- #
# 20 risk symbols in ONE market (`china`), so market grouping cannot be the reason a book-based
# implementation differs -- only the symbol-set source can be.
_R6_UNIVERSE_RISKY = [
    "600000.SH", "600036.SH", "601398.SH", "601288.SH", "600519.SH",
    "000001.SZ", "000002.SZ", "000651.SZ", "002415.SZ", "300750.SZ",
    "600276.SH", "601318.SH", "600887.SH", "601166.SH", "600030.SH",
    "600104.SH", "601668.SH", "600900.SH", "002594.SZ", "300059.SZ",
]
# Present in the loaded universe, and MUST NOT be counted in N_t (`r6.3`: "minus cash proxies").
_R6_CASH_PROXIES_IN_UNIVERSE = ("511880.SH", "BIL")


def _write_universe_list(tmp_path, name, symbols):
    path = tmp_path / name
    path.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    return path


def _install_book_restriction(monkeypatch):
    """Restrict the EqualWeight target book to `state["keep"]` (columns dropped, never zeroed).

    Returns a mutable state dict so ONE installed mock can serve several runs with different books
    (re-installing a second mock would wrap the first, and the two drops would intersect). Dropping
    (rather than zeroing) matters: a zeroed column stays in `current_targets.index` and would still
    look like a "book member" to a book-based cross-section, which is exactly the implementation
    this test has to be able to fail against.
    """
    from common.allocation_templates import EqualWeightAllocation

    original = EqualWeightAllocation.generate_weights
    state = {"keep": None}

    def mock_generate_weights(self, universe, params=None):
        df = original(self, universe, params)
        keep = state["keep"]
        if keep is None:
            return df
        return df.drop(columns=[c for c in df.columns if c not in keep])

    monkeypatch.setattr(EqualWeightAllocation, "generate_weights", mock_generate_weights)
    return state


def _spy_gap_cross_sections(monkeypatch):
    """Record every symbol set handed to `compute_gap_series`, then compute the REAL gap.

    The real computation is kept on purpose: the invariance assertion must be about the numbers
    the judgement actually consumes (`gap` -> `sigma_gap` -> `baseline_median` -> `status`), not
    about a stubbed constant.
    """
    import scripts.run_live_strategy as runner

    real = runner.compute_gap_series
    calls: list[list[str]] = []

    def spy(universe, as_of_date):
        calls.append(sorted(str(s) for s in universe))
        return real(universe, as_of_date)

    monkeypatch.setattr(runner, "compute_gap_series", spy)
    return calls


def _r6_stage2_args(universe_file, out_dir, as_of="2024-05-10", start="2023-01-01"):
    return [
        "--strategy", "equal_weight",
        "--universe-file", str(universe_file),
        "--output-dir", str(out_dir),
        "--as-of-date", as_of,
        "--start-date", start,
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--no-cache",
    ]


def test_r63_cross_section_is_the_universe_and_ignores_the_target_book(tmp_path, monkeypatch):
    """Two runs, SAME universe/history, DIFFERENT books => identical evidence and N_t.

    This is the load-bearing regression for the recorded defect: a 2-name book used to yield a
    2-name sigma, so the trigger flipped on strategy rotation rather than on risk. Reverting
    `scripts/run_live_strategy.py` to `group_universe = {s: universe[s] for s in group_symbols ...}`
    turns this test red on `calls[0] != calls[1]` (and on `symbols_used`).
    """
    import json

    import scripts.run_live_strategy as runner

    universe_file = _write_universe_list(
        tmp_path, "univ_r63.txt", _R6_UNIVERSE_RISKY + list(_R6_CASH_PROXIES_IN_UNIVERSE)
    )
    calls = _spy_gap_cross_sections(monkeypatch)
    book_state = _install_book_restriction(monkeypatch)

    books = {
        "book_a": {"600000.SH", "601398.SH"},
        "book_b": {"600036.SH", "000001.SZ", "600519.SH"},
    }
    summaries = {}
    for tag, keep in books.items():
        book_state["keep"] = keep
        out_dir = tmp_path / tag
        runner.main(_r6_stage2_args(universe_file, out_dir))
        summaries[tag] = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    # (10) The cross-section is the 20 RISK symbols of the loaded universe ...
    assert len(calls) == 2, calls
    assert calls[0] == calls[1] == sorted(_R6_UNIVERSE_RISKY)
    # (12) ... and the cash proxies present in the universe never enter it. `N_t == 20` (not 22)
    # is the same fact seen through the judgement's own evidence field.
    for proxy in _R6_CASH_PROXIES_IN_UNIVERSE:
        assert proxy not in calls[0]

    # (11) Rotation invariance: nothing the judgement reports may move when only the book moves.
    a = summaries["book_a"]["overnight_derisk"]
    b = summaries["book_b"]["overnight_derisk"]
    for key in (
        "status",
        "sigma_gap",
        "baseline_median",
        "sessions_used",
        "symbols_used",
        "decision_instant",
        "ratio_applied",
    ):
        assert a[key] == b[key], f"{key} moved with the target book: {a[key]!r} vs {b[key]!r}"
    assert a["status"] in STATUSES
    # The whole point: the reading is over the universe, not over today's two names.
    assert a["symbols_used"] == len(_R6_UNIVERSE_RISKY) == 20
    # A real, finite sigma over the 20-name cross-section (NOT `None`): the invariance above is
    # therefore compared on a number, not on two `None`s.
    assert a["sigma_gap"] is not None and a["sigma_gap"] > 0.0
    assert a["sessions_used"] > DEFAULT_GAP_WINDOW
    # The books really did differ (otherwise the invariance would be vacuous).
    assert books["book_a"] != books["book_b"]


def test_r63_cash_proxy_resolution_covers_universe_spellings():
    """The exclusion is by bare code, because universes spell `511880.SH` while the set stores `511880`."""
    import scripts.run_live_strategy as runner

    resolved = runner.resolve_cash_proxy_target_symbols(
        _R6_UNIVERSE_RISKY + ["511880.SH", "GC001", "BIL"], "BIL"
    )
    assert resolved == {"511880.SH", "GC001", "BIL"}
    assert not (resolved & set(_R6_UNIVERSE_RISKY))


def test_r63_empty_cross_section_is_insufficient_data_never_a_book_fallback():
    """An EMPTY market cross-section is `INSUFFICIENT_DATA`, never a book fallback.

    This is the branch `scripts/run_live_strategy.py` relies on when `cross_section_markets` has no
    entry for a book's market: it passes `{}` (never the book), and the judgement must report
    "cannot decide" rather than invent a sigma from whatever names happen to be held. The branch is
    pinned here at the module boundary because the runner's book is always a subset of the loaded
    universe (so an empty group cannot be reached end-to-end without a contrived symbol mapping).
    """
    empty_gap, empty_used = compute_gap_series({}, pd.Timestamp("2024-05-10"))
    assert empty_gap.empty and empty_used.empty

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        decision = evaluate_overnight_derisk(
            empty_gap, empty_used, "china", dt.date(2024, 5, 10)
        )
    assert decision.status is OvernightDeriskStatus.INSUFFICIENT_DATA
    assert decision.ratio == RATIO_UNCHANGED
    assert decision.sigma_gap is None and decision.baseline_median is None
    assert decision.sessions_used == 0 and decision.symbols_used == 0
    assert apply_overnight_downscale(_targets(), decision, {"BIL"}) == _targets()
    assert len([w for w in caught if issubclass(w.category, RuntimeWarning)]) == 1


def test_stage2_triggered_downsize_turns_a_held_winner_into_a_sell(tmp_path, monkeypatch):
    """`r6.3` consumed end-to-end for a SELL: the de-risked target is what the ticket sells down to.

    The reviewer noted only BUY rows were covered. The position is deliberately held at 35% of NAV
    against a raw equal-weight target of 50%: WITHOUT the de-risk that is a BUY (+15%, above the 5%
    inertia band), and only the TRIGGERED 0.5 multiplier (50% -> 25%) turns it into a SELL. So the
    direction flip itself is the assertion -- the multiplier is applied BEFORE the order math, not
    layered on afterwards.
    """
    import json

    import scripts.run_live_strategy as runner

    universe_file = _write_universe_list(tmp_path, "univ_sell.txt", ["600000.SH", "601398.SH"])

    # Fractional-weight holdings (the runner's "weight mode": a non-integral value <= 1.0), so the
    # reference book is exactly 35% of NAV and the marker math below is not dependent on share
    # rounding. 35% sits strictly between the de-risked 25% and the raw 50% target.
    held_weight = 0.35

    gap = _gap_series(spike=0.06, end="2024-05-10")
    monkeypatch.setattr(
        runner,
        "compute_gap_series",
        lambda universe, as_of_date: (gap, _symbols_used(gap)),
    )

    def _run(tag, extra):
        state_file = tmp_path / f"state_{tag}.json"
        state_file.write_text(json.dumps({"holding_bars": {"600000.SH": 60}}), encoding="utf-8")
        out_dir = tmp_path / tag
        runner.main(
            _r6_stage2_args(universe_file, out_dir)
            + [
                "--account-state-file", str(state_file),
                "--current-holdings", json.dumps({"600000.SH": held_weight}),
                "--exit-confirm-bars", "1",
            ]
            + list(extra)
        )
        payload = json.loads((out_dir / "live_trading_ticket.json").read_text(encoding="utf-8"))
        return {r["symbol"]: r for r in payload["orders"]}

    rows = _run("derisked", [])
    row = rows["600000.SH"]
    assert row["action"] == "SELL", f"expected a downsizing SELL, got {row['action']} ({row.get('reason')})"
    # Weight truth: the de-risked target, not the raw 0.5.
    assert row["target_weight"] == pytest.approx(0.25)
    assert row["detail_reason"]["overnight_derisk"]["status"] == "TRIGGERED"
    assert row["detail_reason"]["overnight_derisk"]["ratio_applied"] == 0.5
    # Shares truth: a partial trim (not a liquidation) down to the de-risked weight.
    assert int(row["current_shares"]) > 0
    assert int(row["target_shares"]) > 0
    assert int(row["delta_shares"]) < 0
    assert row["current_weight"] == pytest.approx(held_weight, abs=0.01)

    # CONTROL: the same holdings, the same book, the flag off => the same delta is a BUY.
    control = _run("control", ["--no-enable-overnight-derisk"])["600000.SH"]
    assert control["action"] == "BUY", control.get("reason")
    assert control["target_weight"] == pytest.approx(0.5)
    assert int(control["delta_shares"]) > 0


def test_r63_market_inference_fallback_is_printed_not_silent(tmp_path, monkeypatch, capsys):
    """A symbol whose market cannot be inferred falls back to `clock_ctx.market` LOUDLY.

    No behaviour change: the symbol stays in its (fallback) market group and the judgement
    proceeds. What must not happen is the silent attribution the reviewer flagged -- an unverified
    market means `D` and the cross-section are computed on an assumption, and the operator has to
    see *which* symbols that covers.
    """
    import json

    import scripts.run_live_strategy as runner
    from common.market_clock import UnknownSymbolMarketError

    universe_file = _write_universe_list(tmp_path, "univ_fallback.txt", ["600000.SH", "601398.SH"])
    real_infer = runner.infer_market_from_symbol

    def fake_infer(symbol):
        if str(symbol) in {"600000.SH", "601398.SH"}:
            raise UnknownSymbolMarketError(str(symbol))
        return real_infer(symbol)

    monkeypatch.setattr(runner, "infer_market_from_symbol", fake_infer)

    gap = _gap_series(spike=0.06, end="2024-05-10")
    real_compute = runner.compute_gap_series
    monkeypatch.setattr(
        runner, "compute_gap_series", lambda universe, as_of_date: (gap, _symbols_used(gap))
    )
    out_dir = tmp_path / "fallback"
    runner.main(_r6_stage2_args(universe_file, out_dir))

    printed = capsys.readouterr().out
    assert "[INFO] (R6)" in printed
    assert "600000.SH" in printed and "601398.SH" in printed
    block = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))["overnight_derisk"]
    # Same market as the run, and the judgement still ran: the fallback is diagnostic only.
    assert block["market"] == "china"
    assert block["status"] == "TRIGGERED"

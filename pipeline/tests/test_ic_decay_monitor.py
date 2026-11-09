"""Offline regression suite for `common/ic_decay_monitor.py` (development_plan `AT3`).

`AT3` (docs/development_plan.md §2.24) asks for per-market, per-leg live
Rank IC / ICIR / half-life monitoring that (a) reuses `common/factor_analyzer.py`
instead of reimplementing the math, (b) refuses to score a cross-section below 15
symbols and never fills 0, (c) cannot be backfilled, and (d) states plainly that
the first conclusion needs 10 valid evaluation days plus the horizon.

100% offline: every price panel is generated from a seeded `numpy` RNG, no
network, no real market data, no writes at all.

The synthetic panel is built so the *score* really does predict the forward
5-bar return (score is an AR(1) cross-sectional signal, prices are driven by it),
which is what lets these tests distinguish "IC is positive" from "IC is 0.0
because nothing was measured".
"""

import ast
import inspect
import json
import math
import os
import sys
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common import factor_analyzer as factor_analyzer_mod  # noqa: E402
from common import ic_decay_monitor as monitor_mod  # noqa: E402
from common.ic_decay_monitor import (  # noqa: E402
    FROZEN_HORIZON_BARS,
    FROZEN_RETURN_BASIS,
    BackfilledSnapshotError,
    ICDecayMonitor,
    IncompleteSnapshotError,
    IntradayInformationSetError,
    MonitorInputError,
    evaluate_ic_decay,
)
from common.signal_snapshot import (  # noqa: E402
    SelectionTruncatedSnapshotError,
    SignalSnapshot,
)

HORIZON = FROZEN_HORIZON_BARS  # 5


def _us_symbols(n):
    return [f"AA{i:02d}" for i in range(n)]


def _signal_panel(n_symbols=20, n_dates=40, *, seed=0, phi=0.8, drift=0.004):
    """Seeded price panel whose 5-bar forward return is driven by the score.

    Scores are a cross-sectional AR(1) process (`phi` sets the persistence, hence
    the expected half-life); each day's log return carries `drift * score`, so the
    forward return over the horizon correlates with the score on the same date.
    """
    rng = np.random.default_rng(seed)
    symbols = _us_symbols(n_symbols)
    dates = pd.bdate_range("2026-01-05", periods=n_dates)
    z = np.empty((n_dates, n_symbols))
    z[0] = rng.normal(size=n_symbols)
    for t in range(1, n_dates):
        z[t] = phi * z[t - 1] + math.sqrt(max(1.0 - phi * phi, 0.0)) * rng.normal(size=n_symbols)
    noise = rng.normal(scale=0.0015, size=(n_dates, n_symbols))
    logp = np.empty((n_dates, n_symbols))
    logp[0] = math.log(100.0)
    for t in range(n_dates - 1):
        logp[t + 1] = logp[t] + drift * z[t] + noise[t]
    close = pd.DataFrame(np.exp(logp), index=dates, columns=symbols)
    scores_by_date = {
        dates[i]: {sym: float(z[i, j]) for j, sym in enumerate(symbols)}
        for i in range(n_dates)
    }
    return close, scores_by_date, dates, symbols


def _snapshots(scores_by_date, dates, *, strategy="blend_a", universe_id="us_core",
               candidates=None, recorded_at=None):
    out = []
    for date in dates:
        scores = scores_by_date[date]
        out.append(
            SignalSnapshot(
                date=date,
                strategy=strategy,
                universe_id=universe_id,
                scores=scores,
                candidates=tuple(candidates if candidates is not None else scores.keys()),
                recorded_at=recorded_at if recorded_at is not None else date,
            )
        )
    return out


def _undeclared(snapshot):
    """The same snapshot with its candidate universe omitted (completeness unknown).

    This is the shape a producer gets by simply not passing `candidates=`: the
    scores are still per-symbol, but nothing declares how many names *should* be
    there, so `SignalSnapshot.is_complete` is False.
    """
    return SignalSnapshot(
        date=snapshot.date,
        strategy=snapshot.strategy,
        universe_id=snapshot.universe_id,
        scores=snapshot.scores,
        recorded_at=snapshot.recorded_at,
        source=snapshot.source,
    )


# ---------------------------------------------------------------------------
# 1. delegation to factor_analyzer (no reimplementation)
# ---------------------------------------------------------------------------
def test_computations_delegate_to_factor_analyzer(monkeypatch):
    close, scores_by_date, dates, symbols = _signal_panel()
    calls = {"forward": [], "rank_ic": [], "summary": []}

    real_forward = factor_analyzer_mod.compute_forward_returns
    real_rank_ic = factor_analyzer_mod.compute_rank_ic
    real_summary = factor_analyzer_mod.factor_ic_summary

    def spy_forward(close_df, periods=(1, 5, 21)):
        calls["forward"].append({"periods": periods, "columns": list(close_df.columns)})
        return real_forward(close_df, periods=periods)

    def spy_rank_ic(factor_df, forward_returns_df, min_assets=3):
        calls["rank_ic"].append({"min_assets": min_assets, "shape": factor_df.shape})
        return real_rank_ic(factor_df, forward_returns_df, min_assets=min_assets)

    def spy_summary(factor_df, forward_returns_df, min_assets=3, **kwargs):
        calls["summary"].append({"min_assets": min_assets})
        return real_summary(factor_df, forward_returns_df, min_assets=min_assets, **kwargs)

    monkeypatch.setattr(factor_analyzer_mod, "compute_forward_returns", spy_forward)
    monkeypatch.setattr(factor_analyzer_mod, "compute_rank_ic", spy_rank_ic)
    monkeypatch.setattr(factor_analyzer_mod, "factor_ic_summary", spy_summary)

    report = evaluate_ic_decay(
        _snapshots(scores_by_date, dates[:12]), close, start_date=dates[0]
    )
    leg = report.legs[0]

    assert calls["forward"], "forward returns must come from factor_analyzer"
    assert calls["forward"][0]["periods"] == HORIZON
    assert calls["rank_ic"], "Rank IC must come from factor_analyzer"
    assert calls["summary"], "IC/ICIR/half-life summary must come from factor_analyzer"
    # the 15-symbol floor is forwarded into the shared primitives, not applied
    # by a second, local implementation
    assert calls["rank_ic"][0]["min_assets"] == 15
    assert calls["summary"][0]["min_assets"] == 15
    # half-life is whatever factor_analyzer reported, not a local recomputation
    assert leg.half_life == pytest.approx(
        real_summary(
            pd.DataFrame(
                {
                    s: [
                        scores_by_date[d][s] for d in dates[:12]
                    ]
                    for s in symbols
                },
                index=dates[:12],
            ),
            real_forward(close[symbols], periods=HORIZON).reindex(dates[:12]),
            min_assets=15,
        )["half_life"]
    )


def test_monitor_holds_no_ic_math_of_its_own():
    source = inspect.getsource(monitor_mod)
    for forbidden in ("spearmanr", "corrcoef", "np.log(0.5)", "ln(2)"):
        assert forbidden not in source, f"IC math belongs to factor_analyzer, found {forbidden}"
    # the primitives must be reached through the shared module, so a monkeypatch of
    # `common.factor_analyzer` really changes what this monitor computes
    assert monitor_mod._fa is factor_analyzer_mod
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "common" in imported
    for forbidden in ("subprocess", "requests", "urllib", "scripts", "web"):
        assert forbidden not in imported


# ---------------------------------------------------------------------------
# 2. the 15-symbol floor: INSUFFICIENT_HISTORY, never 0
# ---------------------------------------------------------------------------
def test_cross_section_below_floor_is_insufficient_history_not_zero():
    close, scores_by_date, dates, _ = _signal_panel(n_symbols=14, n_dates=20)
    report = evaluate_ic_decay(_snapshots(scores_by_date, dates[:12]), close, start_date=dates[0])
    leg = report.legs[0]

    assert leg.status == "INSUFFICIENT_HISTORY"
    assert leg.n_snapshot_dates == 12
    assert leg.n_dates_below_cross_section_floor == 12
    assert leg.n_valid_eval_dates == 0
    assert len(leg.ic_series) == 0
    # unmeasured is NaN, never 0.0 (0.0 would claim "measured, no predictive power")
    assert math.isnan(leg.mean_ic)
    assert math.isnan(leg.ic_ir)
    assert math.isnan(leg.half_life)
    assert leg.first_conclusion_available is None
    assert any("not zero-filled" in note for note in leg.notes)
    # the explicit-missing representation survives JSON serialisation as null
    payload = json.dumps(report.to_dict(), allow_nan=False)
    assert '"mean_ic": null' in payload


def test_fifteen_symbols_clear_the_floor_and_produce_a_real_signal():
    close, scores_by_date, dates, symbols = _signal_panel(n_symbols=20, n_dates=40)
    assert len(symbols) == 20
    report = evaluate_ic_decay(_snapshots(scores_by_date, dates[:12]), close, start_date=dates[0])
    leg = report.legs[0]

    assert leg.status == "OK"
    assert leg.horizon_bars == HORIZON == 5
    assert leg.return_basis == FROZEN_RETURN_BASIS == "close_to_close"
    assert leg.min_cross_section == 15
    assert leg.n_valid_eval_dates == 12
    assert leg.n_dates_below_cross_section_floor == 0
    assert leg.mean_ic > 0.5, f"the synthetic panel is built so IC must be strongly positive, got {leg.mean_ic}"
    assert leg.ic_ir > 0
    assert 0.0 < leg.half_life < 252.0
    assert leg.t_stat > 0
    assert leg.advisory is True


def test_inverted_signal_reports_a_negative_ic():
    close, scores_by_date, dates, _ = _signal_panel(n_symbols=20, n_dates=40, drift=-0.004)
    report = evaluate_ic_decay(_snapshots(scores_by_date, dates[:12]), close, start_date=dates[0])
    assert report.legs[0].mean_ic < 0.0


def test_constant_cross_section_is_excluded_not_recorded_as_zero():
    close, scores_by_date, dates, symbols = _signal_panel(n_symbols=20, n_dates=40)
    flat = {date: {sym: 1.0 for sym in symbols} for date in dates}
    report = evaluate_ic_decay(_snapshots(flat, dates[:12]), close, start_date=dates[0])
    leg = report.legs[0]
    # factor_analyzer would call a constant cross-section "IC = 0.0"; the monitor
    # excludes those dates instead and says so.
    assert leg.n_dates_degenerate_cross_section == 12
    assert leg.n_valid_eval_dates == 0
    assert leg.status == "INSUFFICIENT_HISTORY"
    assert math.isnan(leg.mean_ic)


# ---------------------------------------------------------------------------
# 2b. completeness is mandatory at ingest (AT3 condition 2)
# ---------------------------------------------------------------------------
# `docs/development_plan.md` AT3 condition 2: the snapshot must cover every
# candidate, including the ones that were not selected. `SignalSnapshot` only
# enforces that when the producer declares `candidates=`; these tests pin the
# monitor's stricter contract, so a truncated cross-section can never reach the
# IC math (which would be structurally inflated by the selection itself).
def test_undeclared_completeness_is_rejected_at_ingest():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    complete = _snapshots(scores_by_date, dates[:1])[0]
    undeclared = _undeclared(complete)
    assert undeclared.candidates == ()
    assert undeclared.is_complete is False  # same scores, completeness unprovable

    monitor = ICDecayMonitor(close, start_date=dates[0])
    with pytest.raises(IncompleteSnapshotError) as excinfo:
        monitor.add_snapshot(undeclared)

    message = str(excinfo.value)
    assert "completeness" in message and "undeclared" in message
    assert "candidates" in message
    # the error belongs to both contracts: monitor input AND snapshot truncation
    assert isinstance(excinfo.value, MonitorInputError)
    assert isinstance(excinfo.value, SelectionTruncatedSnapshotError)
    # fail loud, never silently drop it and never keep a partial cross-section
    assert monitor.snapshots == ()

    # the one-shot helper refuses it on the same grounds
    with pytest.raises(IncompleteSnapshotError, match="completeness"):
        evaluate_ic_decay([undeclared], close, start_date=dates[0])


def test_selection_truncated_snapshot_is_rejected_at_ingest():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    full = _snapshots(scores_by_date, dates[:1])[0]
    declared = tuple(full.candidates)
    dropped = declared[0]
    truncated_scores = {
        symbol: score for symbol, score in full.scores.items() if symbol != dropped
    }

    # (i) the write-time contract already refuses to *build* one: declared
    # candidates ⊃ scores is a `SelectionTruncatedSnapshotError`.
    with pytest.raises(SelectionTruncatedSnapshotError, match="selection-truncated"):
        SignalSnapshot(
            date=full.date,
            strategy=full.strategy,
            universe_id=full.universe_id,
            scores=truncated_scores,
            candidates=declared,
            recorded_at=full.recorded_at,
        )

    # (ii) ... and ingest refuses a payload that never went through that check
    # (a deserialised/hand-built row set, an older writer, a bypassed validator).
    # The monitor does not trust an upstream invariant for a number it publishes.
    truncated = _snapshots(scores_by_date, dates[:1])[0]
    object.__setattr__(truncated, "scores", MappingProxyType(truncated_scores))
    assert truncated.is_complete is False
    assert len(truncated.candidates) == len(truncated_scores) + 1

    monitor = ICDecayMonitor(close, start_date=dates[0])
    with pytest.raises(IncompleteSnapshotError) as excinfo:
        monitor.add_snapshot(truncated)
    message = str(excinfo.value)
    assert "selection-truncated" in message
    assert dropped in message  # names the missing candidate, so the fix is actionable
    assert monitor.snapshots == ()


def test_complete_snapshot_still_flows_through_with_unchanged_ic():
    close, scores_by_date, dates, symbols = _signal_panel(n_symbols=20, n_dates=40)
    snapshots = _snapshots(scores_by_date, dates[:12])
    assert all(snapshot.is_complete for snapshot in snapshots)

    monitor = ICDecayMonitor(close, start_date=dates[0])
    assert monitor.add_snapshots(snapshots) == 12
    leg = monitor.evaluate().legs[0]
    assert leg.status == "OK"
    assert leg.n_valid_eval_dates == 12
    assert leg.mean_ic > 0.5  # the synthetic panel really does carry the signal

    # The guard must not perturb the number: recompute the same IC straight from
    # the shared primitives, bypassing the monitor entirely.
    panel = pd.DataFrame({snapshot.date: dict(snapshot.scores) for snapshot in snapshots}).T
    panel.index = pd.DatetimeIndex(panel.index)
    panel = panel.reindex(columns=symbols)
    forward = factor_analyzer_mod.compute_forward_returns(close, periods=HORIZON)
    forward = forward.reindex(index=panel.index, columns=symbols)
    reference = factor_analyzer_mod.factor_ic_summary(panel, forward, min_assets=15)

    assert leg.mean_ic == pytest.approx(float(reference["mean_ic"]), rel=1e-12)
    assert leg.ic_ir == pytest.approx(float(reference["ic_ir"]), rel=1e-12)
    assert leg.half_life == pytest.approx(float(reference["half_life"]), rel=1e-12)


def test_incomplete_snapshot_is_rejected_before_any_ic_math(monkeypatch):
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    calls = []

    def refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("IC math must not run for a rejected snapshot")

    for name in ("compute_forward_returns", "compute_rank_ic", "factor_ic_summary"):
        monkeypatch.setattr(factor_analyzer_mod, name, refuse)

    complete = _snapshots(scores_by_date, dates[:1])[0]
    monitor = ICDecayMonitor(close, start_date=dates[0])
    with pytest.raises(IncompleteSnapshotError):
        monitor.add_snapshot(_undeclared(complete))
    with pytest.raises(IncompleteSnapshotError):
        evaluate_ic_decay([_undeclared(complete)], close, start_date=dates[0])

    # nothing was ingested, so evaluating empty history touches no primitive either
    assert monitor.evaluate().legs == ()
    assert calls == []


def test_batch_ingest_is_rejected_as_a_unit_when_one_snapshot_is_incomplete():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    monitor = ICDecayMonitor(close, start_date=dates[0])
    good = _snapshots(scores_by_date, dates[:3])
    bad = _undeclared(_snapshots(scores_by_date, dates[3:4])[0])

    with pytest.raises(IncompleteSnapshotError):
        monitor.add_snapshots(good + [bad])
    # a half-ingested batch would be a cross-section the caller believes is complete
    assert monitor.snapshots == ()

    assert monitor.add_snapshots(good) == 3
    assert [snapshot.date for snapshot in monitor.snapshots] == list(dates[:3])


# ---------------------------------------------------------------------------
# 3. no backfill, no intraday mixing
# ---------------------------------------------------------------------------
def test_snapshot_before_start_date_is_rejected_as_backfill():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    monitor = ICDecayMonitor(close, start_date=dates[5])
    with pytest.raises(BackfilledSnapshotError, match="precedes the monitor start"):
        monitor.add_snapshot(_snapshots(scores_by_date, dates[4:5])[0])


def test_snapshot_recorded_long_after_its_session_is_rejected_as_backfill():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    late = _snapshots(
        {dates[0]: scores_by_date[dates[0]]},
        [dates[0]],
        recorded_at=dates[0] + pd.Timedelta(days=5),
    )[0]
    monitor = ICDecayMonitor(close, start_date=dates[0])
    with pytest.raises(BackfilledSnapshotError, match="reconstruction"):
        monitor.add_snapshot(late)
    # a next-morning flush is inside the documented 1-day tolerance
    ok = _snapshots(
        {dates[0]: scores_by_date[dates[0]]},
        [dates[0]],
        recorded_at=dates[0] + pd.Timedelta(hours=20),
    )[0]
    monitor.add_snapshot(ok)


def test_intraday_information_set_is_rejected():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    intraday = SignalSnapshot(
        date=dates[0],
        strategy="blend_a",
        universe_id="us_core",
        scores=scores_by_date[dates[0]],
        candidates=tuple(scores_by_date[dates[0]]),
        information_set="intraday",
        recorded_at=dates[0] + pd.Timedelta(hours=14, minutes=15),
    )
    monitor = ICDecayMonitor(close, start_date=dates[0])
    with pytest.raises(IntradayInformationSetError) as excinfo:
        monitor.add_snapshot(intraday)
    message = str(excinfo.value)
    assert "14:15" in message and "look-ahead" in message


# ---------------------------------------------------------------------------
# 4. first conclusion timing
# ---------------------------------------------------------------------------
def test_first_conclusion_needs_ten_valid_days_plus_the_horizon():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=40)

    nine = evaluate_ic_decay(_snapshots(scores_by_date, dates[:9]), close, start_date=dates[0])
    assert nine.legs[0].n_valid_eval_dates == 9
    assert nine.legs[0].status == "INSUFFICIENT_HISTORY"
    assert nine.legs[0].first_conclusion_available is None

    ten = evaluate_ic_decay(_snapshots(scores_by_date, dates[:10]), close, start_date=dates[0])
    leg = ten.legs[0]
    assert leg.status == "OK"
    expected = close.index[close.index.get_loc(dates[9]) + HORIZON]
    assert leg.first_conclusion_available == expected
    # strictly after the 10th valid day, by the full horizon
    assert leg.first_conclusion_available > dates[9]
    assert close.index.get_loc(leg.first_conclusion_available) - close.index.get_loc(dates[9]) == HORIZON

    # when the price panel stops inside the horizon, the date is not fabricated
    short_close = close.loc[: dates[11]]
    short = evaluate_ic_decay(
        _snapshots({d: scores_by_date[d] for d in dates[6:16]}, dates[6:16]),
        short_close,
        start_date=dates[6],
    )
    assert short.legs[0].first_conclusion_available is None


# ---------------------------------------------------------------------------
# 5. per-market / per-leg separation
# ---------------------------------------------------------------------------
def test_per_market_and_per_leg_are_evaluated_independently():
    close_us, scores_us, dates, _ = _signal_panel(n_symbols=20, n_dates=40, seed=1)
    close_cn, scores_cn, _, cn_symbols = _signal_panel(n_symbols=20, n_dates=40, seed=2, drift=-0.004)
    cn_map = {old: f"{600000 + i}.SH" for i, old in enumerate(cn_symbols)}
    close_cn = close_cn.rename(columns=cn_map)
    scores_cn = {
        date: {cn_map[old]: value for old, value in row.items()}
        for date, row in scores_cn.items()
    }
    close = pd.concat([close_us, close_cn], axis=1)

    snapshots = _snapshots(scores_us, dates[:12], strategy="blend_a", universe_id="us_core")
    snapshots += _snapshots(scores_us, dates[:12], strategy="blend_b", universe_id="us_core")
    snapshots += _snapshots(scores_cn, dates[:12], strategy="blend_a", universe_id="cn_core")

    report = evaluate_ic_decay(snapshots, close, start_date=dates[0])
    keys = sorted(leg.key for leg in report.legs)
    assert keys == [
        ("china", "cn_core", "blend_a"),
        ("us", "us_core", "blend_a"),
        ("us", "us_core", "blend_b"),
    ]
    us_ic = report.leg("us", "us_core", "blend_a").mean_ic
    cn_ic = report.leg("china", "cn_core", "blend_a").mean_ic
    assert us_ic > 0.5 and cn_ic < 0.0, "each market's IC must come from its own cross-section"

    # a cross-section mixing two markets is refused: the session clocks differ
    mixed = dict(scores_us[dates[0]])
    mixed.update(scores_cn[dates[0]])
    monitor = ICDecayMonitor(close, start_date=dates[0])
    with pytest.raises(MonitorInputError, match="mixes markets"):
        monitor.market_of_snapshot(
            SignalSnapshot(
                date=dates[0],
                strategy="blend_a",
                universe_id="mixed",
                scores=mixed,
                candidates=tuple(mixed),
                recorded_at=dates[0],
            )
        )


def test_duplicate_snapshot_for_one_cell_is_rejected():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=20)
    monitor = ICDecayMonitor(close, start_date=dates[0])
    snapshot = _snapshots(scores_by_date, dates[:1])[0]
    monitor.add_snapshot(snapshot)
    with pytest.raises(MonitorInputError, match="duplicate snapshot"):
        monitor.add_snapshot(snapshot)


def test_unknown_market_symbol_fails_loud_and_market_map_is_the_escape_hatch():
    close, scores_by_date, dates, symbols = _signal_panel(n_symbols=20, n_dates=40)
    # 8-digit bare numerics are a shape `infer_market_from_symbol` explicitly
    # refuses (A-shares are 6 digits, HK 4-5): no counter may be attributed to a
    # market by guesswork.
    unmapped = [f"{90000000 + i}" for i in range(len(symbols))]
    symbol_map = dict(zip(symbols, unmapped))
    close2 = close.rename(columns=symbol_map)
    scores2 = {
        date: {symbol_map[old]: value for old, value in row.items()}
        for date, row in scores_by_date.items()
    }

    monitor = ICDecayMonitor(close2, start_date=dates[0])
    with pytest.raises(MonitorInputError, match="market_map"):
        monitor.add_snapshots(_snapshots(scores2, dates[:1]))
        monitor.evaluate()

    monitor = ICDecayMonitor(
        close2, start_date=dates[0], market_map={sym: "us" for sym in unmapped}
    )
    monitor.add_snapshots(_snapshots(scores2, dates[:12]))
    report = monitor.evaluate()
    assert report.legs[0].market == "us"
    assert report.legs[0].status == "OK"


# ---------------------------------------------------------------------------
# 6. missing observations stay missing
# ---------------------------------------------------------------------------
def test_missing_price_cell_is_nan_not_zero_fill(monkeypatch):
    close, scores_by_date, dates, symbols = _signal_panel(n_symbols=20, n_dates=40)
    close.iloc[3, 0] = np.nan  # one symbol has no close on one session
    captured = {}

    real_rank_ic = factor_analyzer_mod.compute_rank_ic

    def spy(factor_df, forward_returns_df, min_assets=3):
        captured["panel"] = factor_df.copy()
        captured["returns"] = forward_returns_df.copy()
        return real_rank_ic(factor_df, forward_returns_df, min_assets=min_assets)

    monkeypatch.setattr(factor_analyzer_mod, "compute_rank_ic", spy)
    report = evaluate_ic_decay(
        _snapshots(scores_by_date, dates[:12]), close, start_date=dates[0]
    )
    panel = captured["panel"]
    # the score survived; the *return* for that cell is NaN and stays NaN.
    assert panel.isna().sum().sum() == 0
    assert captured["returns"].isna().sum().sum() >= 1
    # no zero was written anywhere in the score panel we handed to the IC math
    assert not (panel.to_numpy() == 0.0).any()
    assert report.legs[0].status == "OK"


# ---------------------------------------------------------------------------
# 7. report surface
# ---------------------------------------------------------------------------
def test_report_to_dict_is_strict_json_and_declares_the_frozen_basis():
    close, scores_by_date, dates, _ = _signal_panel(n_dates=40)
    report = evaluate_ic_decay(_snapshots(scores_by_date, dates[:12]), close, start_date=dates[0])
    payload = report.to_dict()
    assert payload["advisory"] is True
    assert payload["horizon_bars"] == 5
    assert payload["return_basis"] == "close_to_close"
    assert payload["min_cross_section"] == 15
    assert payload["min_valid_eval_days"] == 10
    text = json.dumps(payload, allow_nan=False)  # no NaN/Infinity may leak
    assert "NaN" not in text
    leg = payload["legs"][0]
    assert leg["status"] == "OK"
    assert len(leg["ic_series"]) == 12
    assert leg["first_conclusion_available"] is not None
    assert any("advisory" in note for note in leg["notes"])


def test_monitor_rejects_unusable_configuration():
    close, _, dates, _ = _signal_panel(n_dates=20)
    with pytest.raises(MonitorInputError):
        ICDecayMonitor(close, horizon_bars=0)
    with pytest.raises(MonitorInputError):
        ICDecayMonitor(close, min_cross_section=2)
    with pytest.raises(MonitorInputError):
        ICDecayMonitor(close, max_recording_lag_days=-1)
    with pytest.raises(MonitorInputError):
        ICDecayMonitor(close, start_date="not-a-date")

"""Offline tests for the `R3` synthetic 5m panel and its defect classifier.

Unit under test: `common.intraday_synthetic` (the frozen panel spec, the parameterizable generator
and the reusable `classify_5m_panel`). Everything here is synthetic and local -- no cache, no
network, no `backtests_research/`, no real market data. Sibling of
`common/tests/test_intraday_bar_contract.py`, whose `END_LABELS` grid this module must match.

Anti-tautology discipline (doc `R3` acceptance ④): every assertion below pins a concrete number or
shape that is *derived from the injected parameter* rather than from whatever the implementation
happens to return -- e.g. `missing_days=2, missing_bars_per_day=3` must yield exactly 6 missing bars
and exactly 378 remaining rows, and the hand-built frame in
`test_classifier_on_a_hand_built_frame_pins_every_bucket` asserts each bucket as the literal 1.
Deleting the lunch rule, the duplicate keying, the tz offset check or the 停牌/缺失 split turns these
red -- verified by mutation on 2026-11-05 with a throwaway pytest plugin (4 / 3 / 4 / 2 cases failed
respectively; the plugin lived in /tmp and no repo file was edited).

Acceptance mapping (doc `R3` item 4):
  ①  normal panel -> 48/day, no lunch bars, monotonic, END labels:
      `test_frozen_spec_constants_are_exact`,
      `test_normal_panel_is_48_end_labelled_bars_per_day_with_no_lunch_bars`,
      `test_normal_panel_diagnostics_are_all_zero`
  ②  injected gaps -> counts match exactly, bad bars not silently dropped:
      `test_injected_gaps_hit_the_classified_counts_exactly_and_rows_are_not_silently_dropped`,
      `test_suspended_day_is_not_counted_as_missing`
  ③  injected tz offset / duplicates -> explicit count, never a silent pass:
      `test_tz_offset_injection_is_visible_and_does_not_smear_into_missing`,
      `test_duplicate_timestamps_are_counted_and_rows_kept`,
      `test_naive_timestamps_are_a_tz_mismatch_never_assumed`,
      `test_unparseable_timestamp_raises_instead_of_silently_passing`,
      `test_timezone_parameter_cannot_redefine_the_frozen_zone`
  ④  deleting the underlying feature turns a case red:
      `test_classifier_on_a_hand_built_frame_pins_every_bucket`,
      `test_cross_lunch_injection_lands_inside_lunch_break`,
      `test_short_session_is_missing_not_ok`,
      `test_injection_does_not_move_the_price_path`,
      `test_traded_daily_row_with_zero_bars_is_missing_not_unknown`

Unmapped support tests (no separate acceptance item): `test_determinism_and_seed_sensitivity`,
`test_all_five_injections_land_on_their_own_sessions`, `test_injection_slots_must_stay_disjoint`,
`test_generated_bars_are_gate_shaped`, `test_panel_passes_the_m3_3_gate_end_to_end` -- 20 test
functions in total.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import common.intraday_bar_contract as contract
import common.intraday_synthetic as syn


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _build(**kwargs) -> syn.SyntheticPanel:
    return syn.build_synthetic_5m_panel(**kwargs)


def _counts(panel: syn.SyntheticPanel) -> dict:
    return panel.counts


def _session_rows(panel: syn.SyntheticPanel, symbol: str, day) -> pd.DataFrame:
    # `wall_clock`, not `.dt`: a tz-defect panel carries two UTC offsets, so its ts column is
    # `object` dtype and has no `.dt` accessor (see the module's dtype caveat).
    return panel.bars[(panel.bars["symbol"] == symbol) & (syn.wall_clock(panel.bars["ts"]).dt.date == day)]


# ---------------------------------------------------------------------------------------------
# ① frozen spec + normal panel
# ---------------------------------------------------------------------------------------------
def test_frozen_spec_constants_are_exact():
    """The spec is 48 END-labelled bars/day, 11:30-13:00 empty, Asia/Shanghai, no DST."""
    assert syn.SESSION_BARS_PER_DAY == 48
    assert syn.BAR_MINUTES == 5
    assert syn.BAR_TIMESTAMP_CONVENTION == "END"
    assert syn.SESSION_TIMEZONE == "Asia/Shanghai"
    assert syn.EXPECTED_UTC_OFFSET_MINUTES == 480
    assert (syn.LUNCH_BREAK_START, syn.LUNCH_BREAK_END) == ("11:30", "13:00")

    # 48 and not 49: 240 minutes of auction / 5-minute bars, endpoints half-open.
    minutes = sum(
        (pd.Timestamp(f"2000-01-01 {close}") - pd.Timestamp(f"2000-01-01 {open_}")).seconds // 60
        for open_, close in syn.SESSION_BLOCKS
    )
    assert minutes == 240
    assert minutes // syn.BAR_MINUTES == 48

    assert len(syn.GRID_LABELS) == 48
    assert syn.GRID_LABELS[:3] == ("09:35", "09:40", "09:45")
    assert syn.GRID_LABELS[21:25] == ("11:20", "11:25", "11:30", "13:05")
    assert syn.GRID_LABELS[-2:] == ("14:55", "15:00")
    # The four labels that separate END from START, plus the lunch interior.
    assert "11:30" in syn.GRID_LABELS and "13:00" not in syn.GRID_LABELS
    assert "15:00" in syn.GRID_LABELS and "09:30" not in syn.GRID_LABELS
    for inside_lunch in ("11:35", "12:00", "12:55"):
        assert inside_lunch not in syn.GRID_LABELS

    # Cross-file freeze: the panel grid IS the M3.3 contract grid, element for element.
    assert tuple(syn.GRID_LABELS) == tuple(contract.END_LABELS)
    assert tuple(syn.GRID_LABELS) != tuple(contract.START_LABELS)
    assert contract.EXPECTED_BARS_PER_DAY == syn.SESSION_BARS_PER_DAY
    assert contract.EXPECTED_CONVENTION == syn.BAR_TIMESTAMP_CONVENTION

    spec = syn.panel_spec()
    assert spec["grid_labels"] == list(syn.GRID_LABELS)
    assert spec["bars_per_day"] == 48
    assert spec["timestamp_convention"] == "END"
    assert spec["timezone"] == "Asia/Shanghai"
    assert spec["utc_offset_minutes"] == 480
    assert spec["has_dst"] is False
    assert spec["lunch_break"] == ["11:30", "13:00"]
    assert spec["spec_version"] == "r3.1"


def test_normal_panel_is_48_end_labelled_bars_per_day_with_no_lunch_bars():
    """2 symbols x 3 sessions: 288 rows, 48 per session, strictly increasing, 09:35..15:00."""
    panel = _build(symbols=2, sessions=3, seed=11)
    bars = panel.bars
    assert len(bars) == 2 * 3 * 48 == 288
    assert list(bars.columns) == list(syn.BARS_REQUIRED_COLUMNS)

    per_session = bars.groupby(["symbol", "ts"], sort=False).size()
    assert len(per_session) == 288                      # no duplicated (symbol, ts) keys
    counts = bars.groupby([bars["symbol"], bars["ts"].dt.date]).size()
    assert len(counts) == 6
    assert set(counts.to_list()) == {48}

    times = bars["ts"].dt.strftime("%H:%M")
    for label in ("11:30", "13:05", "15:00", "09:35"):
        assert label in set(times)
    for label in ("09:30", "13:00", "11:35", "12:00", "15:05", "09:25"):
        assert label not in set(times)

    # 11:30 is the 24th label (last of the morning) and 13:05 the 25th: the lunch break sits
    # exactly between them, and NOTHING is labelled in it.
    ordered = sorted(set(times))
    assert len(ordered) == 48
    assert ordered[23] == "11:30" and ordered[24] == "13:05"
    assert not any("11:30" < t < "13:00" for t in ordered)

    # END labelling: the first bar of the day closes the 09:30-09:35 slot.
    assert bars["ts"].iloc[0] == pd.Timestamp("2024-06-03 09:35", tz="Asia/Shanghai")
    assert bars["ts"].iloc[47] == pd.Timestamp("2024-06-03 15:00", tz="Asia/Shanghai")

    # Monotonic within every session (the panel is sorted by symbol then labelled clock).
    for _, group in bars.groupby([bars["symbol"], bars["ts"].dt.date], sort=True):
        assert group["ts"].is_monotonic_increasing
        assert group["ts"].is_unique

    # Zone: uniform Asia/Shanghai, constant +0800 (no DST to fold around).
    assert isinstance(bars["ts"].dtype, pd.DatetimeTZDtype)
    assert str(bars["ts"].dt.tz) == "Asia/Shanghai"
    assert set(bars["ts"].dt.strftime("%z")) == {"+0800"}


def test_normal_panel_diagnostics_are_all_zero():
    """A clean panel must report zero of everything -- not "no exceptions raised"."""
    panel = _build(symbols=2, sessions=3, seed=11)
    c = _counts(panel)
    assert c["bars"] == 288
    assert c["sessions"] == 6
    assert c["missing"] == 0
    assert c["missing_sessions"] == 0
    assert c["duplicate"] == 0
    assert c["duplicate_sessions"] == 0
    assert c["tz_mismatch"] == 0
    assert c["tz_mismatch_sessions"] == 0
    assert c["lunch"] == 0
    assert c["pre_session"] == 0
    assert c["post_session"] == 0
    assert c["off_grid"] == 0
    assert c["suspended"] == 0
    assert c["unknown"] == 0
    assert c["ok_sessions"] == 6
    assert panel.diagnostics["has_defects"] is False
    sessions = panel.sessions
    assert len(sessions) == 6
    assert set(sessions["classification"]) == {"ok"}
    assert set(sessions["observed_bars"]) == {48}
    assert set(sessions["missing_bars"]) == {0}


# ---------------------------------------------------------------------------------------------
# ② injected gaps
# ---------------------------------------------------------------------------------------------
def test_injected_gaps_hit_the_classified_counts_exactly_and_rows_are_not_silently_dropped():
    """2 symbols x 4 sessions, 2 gap-days x 3 missing bars: missing==6, rows==378, daily>0."""
    panel = _build(symbols=2, sessions=4, seed=5, missing_days=2, missing_bars_per_day=3)
    c = _counts(panel)
    assert c["missing"] == 2 * 3 == 6
    assert c["missing_sessions"] == 2
    assert c["duplicate"] == 0 and c["tz_mismatch"] == 0 and c["lunch"] == 0
    assert c["suspended"] == 0
    assert panel.injected["expected_counts"]["missing"] == 6

    # Nothing else vanished: 8 sessions x 48 bars - the 6 injected absences.
    assert len(panel.bars) == 8 * 48 - 6 == 378
    assert c["bars"] == 378

    detail = panel.diagnostics["details"]["missing_days"]
    assert len(detail) == 2
    assert set(detail["expected_bars"]) == {48}
    assert set(detail["observed_bars"]) == {45}
    assert set(detail["missing_bars"]) == {3}
    # ... and the reported sessions are exactly the ones the generator says it gapped.
    slots = {(s, d) for s, d in panel.injected["slots"]["missing"]}
    assert {(r.symbol, r.date.isoformat()) for r in detail.itertuples()} == slots

    # The rows really are short (the diagnostic is not a claim on top of a full panel), and the
    # daily table still shows trading -> 缺失, never 停牌.
    for symbol, day_str in slots:
        day = pd.Timestamp(day_str).date()
        assert len(_session_rows(panel, symbol, day)) == 45
        daily_row = panel.daily[(panel.daily["symbol"] == symbol) & (panel.daily["date"] == day)]
        assert len(daily_row) == 1
        assert float(daily_row["volume"].iloc[0]) > 0.0
        classification = panel.sessions[
            (panel.sessions["symbol"] == symbol) & (panel.sessions["date"] == day)
        ]["classification"].tolist()
        assert classification == ["missing"]


def test_suspended_day_is_not_counted_as_missing():
    """停牌 = daily row with volume 0 and no 5m bars; it must never be booked as 缺失."""
    panel = _build(symbols=1, sessions=3, seed=5, suspended_days=1)
    c = _counts(panel)
    assert c["suspended"] == 1
    assert c["missing"] == 0
    assert c["missing_sessions"] == 0
    assert c["unknown"] == 0
    assert len(panel.bars) == 2 * 48 == 96

    symbol, day_str = panel.injected["slots"]["suspended"][0]
    day = pd.Timestamp(day_str).date()
    assert len(_session_rows(panel, symbol, day)) == 0

    daily_row = panel.daily[(panel.daily["symbol"] == symbol) & (panel.daily["date"] == day)]
    assert len(daily_row) == 1
    # 0.0 (真零成交), not NaN (未采集): the halt is a fact, not a gap in collection.
    assert float(daily_row["volume"].iloc[0]) == 0.0
    assert float(daily_row["amount"].iloc[0]) == 0.0

    classification = panel.sessions[
        (panel.sessions["symbol"] == symbol) & (panel.sessions["date"] == day)
    ].iloc[0]
    assert classification["classification"] == "suspended"
    assert int(classification["missing_bars"]) == 0
    # The suspension is visible in the diagnostics without being a defect.
    assert len(panel.diagnostics["details"]["suspended_days"]) == 1
    assert len(panel.diagnostics["details"]["missing_days"]) == 0


# ---------------------------------------------------------------------------------------------
# ③ tz offset / duplicates / naive / unparseable
# ---------------------------------------------------------------------------------------------
def test_tz_offset_injection_is_visible_and_does_not_smear_into_missing():
    """A +09:00-labelled day is 48 tz_mismatch bars AND zero missing bars (orthogonal axes)."""
    panel = _build(symbols=2, sessions=2, seed=3, tz_offset_days=1, tz_offset="+09:00")
    c = _counts(panel)
    assert c["tz_mismatch"] == 48
    assert c["tz_mismatch_sessions"] == 1
    # The frozen rule: coverage is measured on the labelled wall clock, so the zone defect is
    # NOT double-booked as a coverage defect.
    assert c["missing"] == 0
    assert c["lunch"] == 0 and c["off_grid"] == 0
    assert len(panel.bars) == 4 * 48 == 192

    symbol, day_str = panel.injected["slots"]["tz_offset"][0]
    day = pd.Timestamp(day_str).date()
    rows = _session_rows(panel, symbol, day)
    assert len(rows) == 48
    # Labels are untouched (09:35 is still 09:35) while the instant is 08:35 Shanghai.
    assert rows["ts"].iloc[0].strftime("%H:%M") == "09:35"
    assert rows["ts"].iloc[0].utcoffset() == pd.Timedelta(hours=9)
    assert rows["ts"].iloc[0].tz_convert("Asia/Shanghai").strftime("%H:%M") == "08:35"
    # The subset keeps the parent's object dtype (mixed offsets), so read the zone per row.
    assert {pd.Timestamp(v).utcoffset() for v in rows["ts"]} == {pd.Timedelta(hours=9)}
    # Every other session is still clean and cleanly zoned.
    day_mask = (panel.bars["symbol"] == symbol) & (syn.wall_clock(panel.bars["ts"]).dt.date == day)
    others = panel.bars[~day_mask]
    assert len(others) == 3 * 48
    assert {pd.Timestamp(v).strftime("%z") for v in others["ts"]} == {"+0800"}

    detail = panel.diagnostics["details"]["tz_mismatch_rows"]
    assert len(detail) == 48
    assert set(detail["reason"]) == {"utc_offset_+09:00"}
    classification = panel.sessions[
        (panel.sessions["symbol"] == symbol) & (panel.sessions["date"] == day)
    ]["classification"].tolist()
    assert classification == ["tz_mismatch"]


def test_duplicate_timestamps_are_counted_and_rows_kept():
    """2 extra copies on one session: duplicate==2, both rows still in the frame."""
    panel = _build(symbols=2, sessions=2, seed=4, duplicate_days=1, duplicate_bars_per_day=2)
    c = _counts(panel)
    assert c["duplicate"] == 2
    assert c["duplicate_sessions"] == 1
    assert c["missing"] == 0
    assert c["tz_mismatch"] == 0
    # The duplicated rows were NOT deduplicated away: 4 sessions x 48 + 2 extras.
    assert len(panel.bars) == 4 * 48 + 2 == 194
    assert c["bars"] == 194

    symbol, day_str = panel.injected["slots"]["duplicate"][0]
    day = pd.Timestamp(day_str).date()
    rows = _session_rows(panel, symbol, day)
    assert len(rows) == 50
    assert rows["ts"].nunique() == 48

    detail = panel.diagnostics["details"]["duplicate_rows"]
    assert len(detail) == 4          # 2 keys x 2 copies, all reported, none collapsed
    assert set(detail["dup_copies"]) == {2}
    assert set(detail["reason"]) == {"duplicate_timestamp"}
    classification = panel.sessions[
        (panel.sessions["symbol"] == symbol) & (panel.sessions["date"] == day)
    ]["classification"].tolist()
    assert classification == ["duplicate"]


def test_naive_timestamps_are_a_tz_mismatch_never_assumed():
    """Naive labels are ambiguous -> 288 mismatches by default, 0 once the zone is declared."""
    panel = _build(symbols=2, sessions=3, seed=9)
    naive = panel.bars.copy()
    naive["ts"] = naive["ts"].dt.tz_localize(None)
    assert not isinstance(naive["ts"].dtype, pd.DatetimeTZDtype)

    assumed_worst = syn.classify_5m_panel(naive, daily=panel.daily)
    assert assumed_worst["tz_mismatch"] == 288
    assert assumed_worst["tz_mismatch_sessions"] == 6
    assert set(assumed_worst["details"]["tz_mismatch_rows"]["reason"]) == {"naive_timestamp"}
    # Coverage is still measured, and still clean: refusing the zone is not a coverage claim.
    assert assumed_worst["missing"] == 0

    declared = syn.classify_5m_panel(naive, daily=panel.daily, assume_naive_tz="Asia/Shanghai")
    assert declared["tz_mismatch"] == 0
    assert declared["missing"] == 0
    assert declared["bars"] == 288


def test_timezone_parameter_cannot_redefine_the_frozen_zone():
    """The spec zone is +08:00 with no DST; a different zone is refused, not re-interpreted."""
    panel = _build(symbols=1, sessions=1, seed=1)
    assert syn.classify_5m_panel(panel.bars, daily=panel.daily,
                                 timezone="Asia/Shanghai")["tz_mismatch"] == 0
    for wrong in ("UTC", "Asia/Tokyo", "Europe/London"):
        with pytest.raises(syn.PanelSpecError, match="frozen"):
            syn.classify_5m_panel(panel.bars, daily=panel.daily, timezone=wrong)
        with pytest.raises(syn.PanelSpecError, match="frozen"):
            _build(symbols=1, sessions=1, seed=1, timezone=wrong)


def test_unparseable_timestamp_raises_instead_of_silently_passing():
    """A NaT would silently drop out of every count, so it is refused at the door."""
    panel = _build(symbols=1, sessions=1, seed=2)
    broken = panel.bars.copy()
    broken["ts"] = broken["ts"].astype(object)   # so the bad value survives assignment
    broken.loc[broken.index[3], "ts"] = "not-a-timestamp"
    with pytest.raises(syn.PanelSpecError, match="unparseable bar timestamp"):
        syn.classify_5m_panel(broken, daily=panel.daily)

    empty = panel.bars.copy()
    empty.loc[empty.index[0], "ts"] = pd.NaT
    with pytest.raises(syn.PanelSpecError, match="NaT"):
        syn.classify_5m_panel(empty, daily=panel.daily)

    missing_column = panel.bars.drop(columns=["ts"])
    with pytest.raises(syn.PanelSpecError, match="missing required column"):
        syn.classify_5m_panel(missing_column, daily=panel.daily)


# ---------------------------------------------------------------------------------------------
# ④ mutating the underlying feature turns these red
# ---------------------------------------------------------------------------------------------
def test_classifier_on_a_hand_built_frame_pins_every_bucket():
    """9 hand-written rows, every bucket pinned to the literal 1 -- no generator involved."""
    sh = "Asia/Shanghai"
    rows = [
        # symbol A, 2024-06-03
        ("A", pd.Timestamp("2024-06-03 09:00", tz=sh)),   # pre_session
        ("A", pd.Timestamp("2024-06-03 11:35", tz=sh)),   # lunch interior
        ("A", pd.Timestamp("2024-06-03 15:30", tz=sh)),   # post_session
        ("A", pd.Timestamp("2024-06-03 10:07", tz=sh)),   # off_grid (not pre/lunch/post)
        ("A", pd.Timestamp("2024-06-03 13:05", tz=sh)),   # on grid
        ("A", pd.Timestamp("2024-06-03 13:05", tz=sh)),   # duplicate of the previous line
        ("A", pd.Timestamp("2024-06-03 09:35")),          # naive -> tz_mismatch, on grid
        ("A", pd.Timestamp("2024-06-03 09:40", tz="+09:00")),  # off-offset -> tz_mismatch
        # symbol B shares A's 13:05 instant: a duplicate key is (symbol, ts), not ts alone
        ("B", pd.Timestamp("2024-06-03 13:05", tz=sh)),
    ]
    frame = pd.DataFrame({
        "symbol": [r[0] for r in rows],
        "ts": [r[1] for r in rows],
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 1.0,
    })

    result = syn.classify_5m_panel(frame, daily=None)
    assert result["bars"] == 9
    assert result["sessions"] == 2
    assert result["pre_session"] == 1
    assert result["lunch"] == 1
    assert result["post_session"] == 1
    assert result["off_grid"] == 1
    assert result["duplicate"] == 1          # NOT 2: B/13:05 is a different symbol's bar
    assert result["duplicate_sessions"] == 1
    assert result["tz_mismatch"] == 2        # the naive row + the +09:00 row
    assert result["tz_mismatch_sessions"] == 1
    # distinct on-grid bars: A has 13:05, 09:35, 09:40 = 3 -> 45 missing; B has 13:05 -> 47.
    assert result["missing"] == 45 + 47 == 92
    assert result["missing_sessions"] == 2
    # No daily table at all -> nothing can be called halted.
    assert result["suspended"] == 0
    assert result["unknown"] == 0
    # Rows are all still there: nothing deduplicated, nothing filtered.
    assert len(result["details"]["duplicate_rows"]) == 2
    assert len(result["details"]["tz_mismatch_rows"]) == 2
    assert len(result["details"]["lunch_rows"]) == 1
    assert len(result["details"]["off_grid_rows"]) == 3      # pre + post + the 10:07 bar


def test_traded_daily_row_with_zero_bars_is_missing_not_unknown():
    """A day whose daily row traded but whose 5m feed is empty is 缺失, never "cannot tell"."""
    # `build_synthetic_5m_panel` cannot emit this shape -- its gaps drop only `missing_bars_per_day`
    # rows from an otherwise full day and keep `volume > 0` -- so the classifier's DISCLOSED
    # DEVIATION (a zero-bar day with no daily row at all, and only that, is `unknown`) is pinned on
    # frames built by hand. Both zero-bar branches differ in exactly one field: the daily volume.
    sh = "Asia/Shanghai"
    bars = pd.DataFrame({
        "symbol": ["B"] * 48,
        "ts": [pd.Timestamp(f"2024-06-04 {label}", tz=sh) for label in syn.GRID_LABELS],
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 1.0,
    })
    day = dt.date(2024, 6, 3)

    def daily_rows(volume: float) -> pd.DataFrame:
        return pd.DataFrame({
            "symbol": ["A"], "date": [day],
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume": [volume], "amount": [volume],
        })

    # Case A: it traded (volume > 0) and shipped no bars -> the full 48-bar deficit, booked 缺失.
    traded = syn.classify_5m_panel(bars, daily=daily_rows(1_000.0))
    assert traded["bars"] == 48
    assert traded["sessions"] == 2
    assert traded["missing"] == 48
    assert traded["missing_sessions"] == 1
    assert traded["suspended"] == 0
    assert traded["unknown"] == 0
    detail = traded["details"]["missing_days"]
    assert len(detail) == 1
    assert detail["symbol"].tolist() == ["A"]
    assert detail["date"].tolist() == [day]
    assert int(detail["expected_bars"].iloc[0]) == 48
    assert int(detail["observed_bars"].iloc[0]) == 0
    assert int(detail["missing_bars"].iloc[0]) == 48
    assert traded["details"]["suspended_days"].empty
    assert traded["details"]["sessions"].set_index("symbol")["classification"].to_dict() == {
        "A": "missing", "B": "ok"}

    # Case B: the contrast that makes Case A mean something -- volume 0.0 and no bars is 停牌, and a
    # halt must never be booked as 缺失.
    halted = syn.classify_5m_panel(bars, daily=daily_rows(0.0))
    assert halted["suspended"] == 1
    assert halted["missing"] == 0
    assert halted["missing_sessions"] == 0
    assert halted["unknown"] == 0
    assert halted["details"]["missing_days"].empty
    assert halted["details"]["suspended_days"]["symbol"].tolist() == ["A"]
    assert halted["details"]["sessions"].set_index("symbol")["classification"].to_dict() == {
        "A": "suspended", "B": "ok"}


def test_cross_lunch_injection_lands_inside_lunch_break():
    """Feed bars inside the halt: lunch==2, on-grid coverage still 48 so missing stays 0."""
    panel = _build(symbols=1, sessions=2, seed=6, cross_lunch_days=1, cross_lunch_bars=2)
    c = _counts(panel)
    assert c["lunch"] == 2
    assert c["off_grid"] == 0            # exclusive bucketing: a lunch bar is not also off-grid
    assert c["missing"] == 0
    assert len(panel.bars) == 2 * 48 + 2 == 98

    symbol, day_str = panel.injected["slots"]["cross_lunch"][0]
    day = pd.Timestamp(day_str).date()
    rows = _session_rows(panel, symbol, day)
    assert len(rows) == 50
    lunch_times = sorted(t for t in rows["ts"].dt.strftime("%H:%M") if "11:30" < t < "13:00")
    assert lunch_times == ["11:35", "11:40"]
    # 11:30 itself is a real END label, so it must NOT be counted as a lunch bar.
    assert "11:30" in set(rows["ts"].dt.strftime("%H:%M"))
    detail = panel.diagnostics["details"]["lunch_rows"]
    assert set(detail["reason"]) == {"bar_inside_lunch_break"}
    classification = panel.sessions[
        (panel.sessions["symbol"] == symbol) & (panel.sessions["date"] == day)
    ]["classification"].tolist()
    assert classification == ["off_grid"]


def test_short_session_is_missing_not_ok():
    """bars_per_day=40 is a systematic 8-bar deficit per session -- reported, never 'ok'."""
    panel = _build(symbols=1, sessions=2, seed=8, bars_per_day=40)
    c = _counts(panel)
    assert len(panel.bars) == 80
    assert c["missing"] == 2 * 8 == 16
    assert c["missing_sessions"] == 2
    assert c["ok_sessions"] == 0
    assert set(panel.sessions["classification"]) == {"missing"}
    assert set(panel.sessions["observed_bars"]) == {40}
    assert panel.diagnostics["has_defects"] is True
    with pytest.raises(syn.PanelSpecError, match="48"):
        _build(symbols=1, sessions=1, bars_per_day=49)


def test_injection_does_not_move_the_price_path():
    """Injecting defects into the time/coverage axes must leave every price bit-identical."""
    clean = _build(symbols=1, sessions=3, seed=21)
    dirty = _build(symbols=1, sessions=3, seed=21, missing_days=1, tz_offset_days=1,
                   duplicate_days=0)
    assert len(dirty.bars) == 3 * 48 - 3

    def keyed(panel):
        f = panel.bars[["close", "volume", "amount"]].copy()
        f["wall"] = syn.wall_clock(panel.bars["ts"]).to_numpy()
        return f.set_index("wall")

    a, b = keyed(clean), keyed(dirty)
    joined = a.join(b, how="inner", lsuffix="_clean", rsuffix="_dirty")
    assert len(joined) == len(b) == 141
    assert (joined["close_clean"] == joined["close_dirty"]).all()
    assert (joined["volume_clean"] == joined["volume_dirty"]).all()
    assert (joined["amount_clean"] == joined["amount_dirty"]).all()

    # The daily table is the regime of record: identical too, despite the 5m gap/tz defects.
    assert clean.daily.equals(dirty.daily)


def test_determinism_and_seed_sensitivity():
    """Same seed -> identical panel; a different seed -> different prices."""
    a = _build(symbols=2, sessions=2, seed=77)
    b = _build(symbols=2, sessions=2, seed=77)
    pd.testing.assert_frame_equal(a.bars, b.bars)
    pd.testing.assert_frame_equal(a.daily, b.daily)
    assert a.counts == b.counts
    pd.testing.assert_frame_equal(a.sessions, b.sessions)
    for key in ("missing_days", "suspended_days", "duplicate_rows", "tz_mismatch_rows",
                "lunch_rows", "off_grid_rows"):
        pd.testing.assert_frame_equal(a.diagnostics["details"][key],
                                      b.diagnostics["details"][key])

    c = _build(symbols=2, sessions=2, seed=78)
    assert len(c.bars) == len(a.bars)
    assert (c.bars["close"].to_numpy() != a.bars["close"].to_numpy()).sum() > 90


def test_all_five_injections_land_on_their_own_sessions():
    """Five defect kinds at once: each lands on its own (symbol, day), each count its own number."""
    panel = _build(symbols=1, sessions=5, seed=12, suspended_days=1, missing_days=1,
                   missing_bars_per_day=3, cross_lunch_days=1, cross_lunch_bars=2,
                   tz_offset_days=1, duplicate_days=1, duplicate_bars_per_day=2)
    slots = panel.injected["slots"]
    flat = [tuple(pair) for kind in slots for pair in slots[kind]]
    assert len(flat) == len(set(flat)) == 5           # disjoint: no session carries two defects

    c = _counts(panel)
    assert (c["missing"], c["duplicate"], c["tz_mismatch"], c["lunch"], c["suspended"]) == (
        3, 2, 48, 2, 1)
    # 5 sessions, one of which is halted (0 bars), minus the 3 dropped, plus 2 duplicates and 2
    # cross-lunch extras: the frame keeps every row it was given.
    assert c["bars"] == 4 * 48 - 3 + 2 + 2 == 193

    details = panel.diagnostics["details"]
    assert {(r.symbol, r.date.isoformat()) for r in details["missing_days"].itertuples()} == set(
        slots["missing"])
    assert {(r.symbol, r.date.isoformat()) for r in details["suspended_days"].itertuples()} == set(
        slots["suspended"])
    assert {(r.symbol, r.date.isoformat()) for r in details["tz_mismatch_rows"].itertuples()} == set(
        slots["tz_offset"])
    assert {(r.symbol, r.date.isoformat()) for r in details["duplicate_rows"].itertuples()} == set(
        slots["duplicate"])
    assert {(r.symbol, r.date.isoformat()) for r in details["lunch_rows"].itertuples()} == set(
        slots["cross_lunch"])
    # The three defects leave the price/volume path alone; only the clock and coverage move.
    assert set(panel.sessions["classification"]) == {
        "missing", "suspended", "tz_mismatch", "duplicate", "off_grid"}
    assert len(panel.sessions) == 5


def test_injection_slots_must_stay_disjoint():
    """Two defects on a one-session panel would make the counts un-hand-checkable -> raise."""
    with pytest.raises(syn.PanelSpecError, match="slots"):
        _build(symbols=1, sessions=1, suspended_days=1, missing_days=1)
    with pytest.raises(syn.PanelSpecError, match="slots"):
        _build(symbols=1, sessions=2, missing_days=3)
    # A gap wider than the session is a missing day, not a gap: refuse instead of slicing weirdly.
    with pytest.raises(syn.PanelSpecError, match="missing_bars_per_day"):
        _build(symbols=1, sessions=1, missing_days=1, missing_bars_per_day=49)


# ---------------------------------------------------------------------------------------------
# the panel is shaped for the M3.3 gate / intraday_features (no amount reconstruction)
# ---------------------------------------------------------------------------------------------
def test_generated_bars_are_gate_shaped():
    """amount inside [low, high] per bar, never a volume*close rebuild, daily = clean sums."""
    panel = _build(symbols=2, sessions=2, seed=31)
    bars = panel.bars
    assert (bars["volume"] > 0).all()
    assert (bars["amount"] > 0).all()
    implied_vwap = bars["amount"] / bars["volume"]
    assert (implied_vwap >= bars["low"] * (1 - 1e-9)).all()
    assert (implied_vwap <= bars["high"] * (1 + 1e-9)).all()
    assert ((bars["low"] <= bars[["open", "close"]].min(axis=1))).all()
    assert ((bars["high"] >= bars[["open", "close"]].max(axis=1))).all()

    # M3.3 item 7: amount must not be volume*close on every row (the gate trips on that).
    rebuilt = np.isclose(bars["amount"], bars["volume"] * bars["close"], rtol=1e-12, atol=0)
    assert not rebuilt.all()
    assert int(rebuilt.sum()) < len(bars)

    # The daily row is the full-session aggregate of the 5m bars.
    daily = panel.daily
    assert len(daily) == 4
    agg = bars.groupby(["symbol", bars["ts"].dt.date]).agg(
        volume=("volume", "sum"), amount=("amount", "sum"))
    for (symbol, day), row in agg.iterrows():
        d = daily[(daily["symbol"] == symbol) & (daily["date"] == day)].iloc[0]
        assert float(d["volume"]) == pytest.approx(float(row["volume"]), rel=1e-6)
        assert float(d["amount"]) == pytest.approx(float(row["amount"]), rel=1e-6)


def test_panel_passes_the_m3_3_gate_end_to_end(tmp_path):
    """The panel is not merely "shaped like" a 5m feed -- the tracked M3.3 gate accepts it.

    This is the reuse proof for `R1` / `R4` / `R6` / `R7`: they can hand these two frames straight
    to `common.intraday_bar_contract.run_gate` without any shimming. It also pins A4's END
    detection and the item-7 amount guard behaviourally (a `volume * close` amount would close the
    gate), and it writes only into pytest's temp dir -- no cache, no network.
    """
    panel = _build(symbols=3, sessions=6, seed=42)
    report, violations = contract.run_gate(panel.bars.copy(), panel.daily.copy(),
                                           out_dir=str(tmp_path))
    assert report["all_passed"] is True
    assert report["exit_code"] == 0
    assert len(violations) == 0
    assert report["assertions"]["A4_timestamp_convention"]["detected"] == "END"
    shape = report["assertions"]["A3_shape"]
    assert shape["days_checked"] == 18
    assert shape["days_ok"] == 18
    assert shape["days_missing"] == 0
    assert shape["days_suspended"] == 0
    assert shape["lunch_bars"] == 0
    assert shape["duplicate_bars"] == 0
    assert report["assertions"]["A1_amount_coverage"]["bars_checked"] == 3 * 6 * 48 == 864
